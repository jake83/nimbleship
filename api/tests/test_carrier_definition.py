"""The Carrier Definition schema (ADR 0009): declarative mappings over
closed vocabularies, validated at authoring time - a definition referencing
unknown facts or transforms must fail before it is ever stored."""

import pytest
from pydantic import ValidationError

from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.engine.field_plugins import FIELD_PLUGINS

MINIMAL = {
    "carrier": "furdeco",
    "name": "Furdeco",
    "auth": {"scheme": "query_key", "param": "key", "secret": "config.api_key"},
    "operations": {
        "book": {
            "steps": [
                {
                    "name": "save",
                    "transport": "http",
                    "request": {
                        "method": "POST",
                        "url": "config.base_url",
                        "content_type": "form",
                        "mapping": [
                            {
                                "target": "order_number",
                                "source": "shipment.order_number",
                            },
                            {
                                "target": "address",
                                "source": "shipment.address_lines",
                                "transform": {"name": "join", "with": ", "},
                            },
                            {"target": "service_level", "const": "2 Man"},
                        ],
                    },
                    "response": {
                        "format": "xml",
                        "extract": [
                            {
                                "name": "tracking_reference",
                                "path": "/response/carrier_reference",
                            }
                        ],
                    },
                }
            ],
            "label": {"source": "local_render"},
        }
    },
}


def test_a_minimal_definition_validates() -> None:
    definition = CarrierDefinition.model_validate(MINIMAL)

    assert definition.carrier == "furdeco"
    step = definition.operations["book"].steps[0]
    assert step.request.mapping[0].source == "shipment.order_number"


def test_unknown_transform_is_rejected_at_authoring() -> None:
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "form",
                            "mapping": [
                                {
                                    "target": "x",
                                    "source": "shipment.order_number",
                                    "transform": {"name": "reticulate"},
                                }
                            ],
                        },
                    }
                ],
            }
        },
    }

    with pytest.raises(ValidationError):
        CarrierDefinition.model_validate(bad)


def test_unknown_source_root_is_rejected_at_authoring() -> None:
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "form",
                            "mapping": [
                                {"target": "x", "source": "shipmnt.order_number"}
                            ],
                        },
                    }
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown source root"):
        CarrierDefinition.model_validate(bad)


def test_mapping_requires_exactly_one_of_source_or_const() -> None:
    for entry in (
        {"target": "x"},
        {"target": "x", "source": "shipment.value", "const": "both"},
    ):
        bad = {
            **MINIMAL,
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "save",
                            "transport": "http",
                            "request": {
                                "method": "POST",
                                "url": "config.base_url",
                                "content_type": "form",
                                "mapping": [entry],
                            },
                        }
                    ],
                }
            },
        }
        with pytest.raises(ValidationError, match="exactly one of"):
            CarrierDefinition.model_validate(bad)


def test_step_outputs_are_valid_sources_for_later_steps() -> None:
    two_step = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "manifest",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "order",
                                    "source": "shipment.order_number",
                                }
                            ],
                        },
                        "response": {
                            "format": "json",
                            "extract": [{"name": "tracking", "path": "trackingCodes"}],
                        },
                    },
                    {
                        "name": "label",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "trackingNumber",
                                    "source": "steps.manifest.tracking",
                                }
                            ],
                        },
                    },
                ],
            }
        },
    }

    definition = CarrierDefinition.model_validate(two_step)

    assert definition.operations["book"].steps[1].request.mapping[0].source == (
        "steps.manifest.tracking"
    )


def test_step_reference_to_unknown_step_is_rejected() -> None:
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "only",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "x", "source": "steps.nope.tracking"}
                            ],
                        },
                    }
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown step"):
        CarrierDefinition.model_validate(bad)


def _with_entries(*entries: dict[str, object]) -> dict[str, object]:
    return {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": list(entries),
                        },
                    }
                ],
            }
        },
    }


class _StaticPlugin:
    def compute(self, facts: dict[str, object]) -> object:
        return "computed"


def test_plugin_entries_validate_when_the_plugin_is_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(FIELD_PLUGINS, "test_static", _StaticPlugin())

    definition = CarrierDefinition.model_validate(
        _with_entries({"target": "number", "plugin": "test_static"})
    )

    entry = definition.operations["book"].steps[0].request.mapping[0]
    assert entry.plugin == "test_static"


def test_unregistered_plugin_names_are_rejected_at_authoring() -> None:
    bad = _with_entries({"target": "number", "plugin": "reticulator"})

    with pytest.raises(ValidationError, match="unknown field plugin 'reticulator'"):
        CarrierDefinition.model_validate(bad)


def test_plugin_is_exclusive_with_source_and_const(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(FIELD_PLUGINS, "test_static", _StaticPlugin())
    entries: list[dict[str, object]] = [
        {"target": "x", "plugin": "test_static", "source": "shipment.order_number"},
        {"target": "x", "plugin": "test_static", "const": "0000001"},
    ]
    for entry in entries:
        with pytest.raises(ValidationError, match="exactly one of"):
            CarrierDefinition.model_validate(_with_entries(entry))


def test_plugin_entries_take_no_transform_each_or_pluck(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(FIELD_PLUGINS, "test_static", _StaticPlugin())
    for extra in (
        {"transform": {"name": "uppercase"}},
        {"each": [{"target": "y", "source": "item.value"}]},
        {"pluck": "value"},
    ):
        entry: dict[str, object] = {"target": "x", "plugin": "test_static", **extra}
        with pytest.raises(ValidationError, match="no transform, each, or pluck"):
            CarrierDefinition.model_validate(_with_entries(entry))


def test_pluck_collects_scalars_and_stands_alone_on_its_source() -> None:
    # A valid pluck entry validates.
    CarrierDefinition.model_validate(
        _with_entries(
            {
                "target": "ssccs",
                "source": "shipment.parcels",
                "pluck": "carrier_barcode",
            }
        )
    )
    # pluck needs a collection source: with none, the exactly-one-origin rule
    # rejects it (pluck is a modifier, not a value origin).
    with pytest.raises(ValidationError, match="exactly one of source"):
        CarrierDefinition.model_validate(
            _with_entries({"target": "ssccs", "pluck": "carrier_barcode"})
        )
    # pluck is exclusive with each and transform.
    for extra in (
        {"each": [{"target": "y", "source": "item.value"}]},
        {"transform": {"name": "uppercase"}},
    ):
        entry: dict[str, object] = {
            "target": "ssccs",
            "source": "shipment.parcels",
            "pluck": "carrier_barcode",
            **extra,
        }
        with pytest.raises(ValidationError, match="pluck takes no each or transform"):
            CarrierDefinition.model_validate(_with_entries(entry))
    # An empty pluck path would render-fail every booking; caught at authoring.
    with pytest.raises(ValidationError, match="pluck needs a field path"):
        CarrierDefinition.model_validate(
            _with_entries(
                {"target": "ssccs", "source": "shipment.parcels", "pluck": "  "}
            )
        )


def test_pluck_is_rejected_in_an_xml_target() -> None:
    with pytest.raises(ValidationError, match="pluck is not supported in xml"):
        CarrierDefinition.model_validate(
            _ftp_step(
                content_type="xml",
                root_element="Order",
                mapping=[
                    {
                        "target": "SSCC",
                        "source": "shipment.parcels",
                        "pluck": "carrier_barcode",
                    }
                ],
            )
        )


def test_conflicting_nested_targets_are_rejected_at_authoring() -> None:
    bad = _with_entries(
        {"target": "consignment", "const": "flat"},
        {"target": "consignment.weight", "source": "shipment.weight_kg"},
    )

    with pytest.raises(ValidationError, match="conflict"):
        CarrierDefinition.model_validate(bad)


def test_a_skipped_list_index_in_a_target_is_rejected_at_authoring() -> None:
    bad = _with_entries({"target": "consignments.1.weight", "source": "shipment.w"})

    with pytest.raises(ValidationError, match="index"):
        CarrierDefinition.model_validate(bad)


def test_auth_secret_source_is_validated_at_authoring() -> None:
    bad = {
        **MINIMAL,
        "auth": {
            "scheme": "query_key",
            "param": "key",
            "secret": "cofnig.api_key",
        },
    }

    with pytest.raises(ValidationError, match="unknown source root"):
        CarrierDefinition.model_validate(bad)


def test_success_when_without_a_path_is_rejected_at_authoring() -> None:
    import copy

    malformed = copy.deepcopy(MINIMAL)
    step = malformed["operations"]["book"]["steps"][0]  # type: ignore[index]
    step["response"]["success_when"] = {"equals": "OK"}

    with pytest.raises(ValidationError, match="path"):
        CarrierDefinition.model_validate(malformed)


def test_error_message_without_a_path_is_rejected_at_authoring() -> None:
    import copy

    malformed = copy.deepcopy(MINIMAL)
    step = malformed["operations"]["book"]["steps"][0]  # type: ignore[index]
    step["response"]["error_message"] = {"pat": "/response/error"}

    with pytest.raises(ValidationError, match="path"):
        CarrierDefinition.model_validate(malformed)


def test_step_output_names_are_validated_at_authoring() -> None:
    """A typo'd OUTPUT name (valid step, wrong extraction) must fail at
    authoring - at execution it would render a placeholder token and send
    it to a live carrier (refuter, PR #30)."""
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "manifest",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "order",
                                    "source": "shipment.order_number",
                                }
                            ],
                        },
                        "response": {
                            "format": "json",
                            "extract": [{"name": "tracking", "path": "codes"}],
                        },
                    },
                    {
                        "name": "label",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "code",
                                    "source": "steps.manifest.trackign",
                                }
                            ],
                        },
                    },
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown output"):
        CarrierDefinition.model_validate(bad)


def test_step_reference_to_a_step_with_no_extractions_is_rejected() -> None:
    bad = {
        **MINIMAL,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "fire",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "order",
                                    "source": "shipment.order_number",
                                }
                            ],
                        },
                    },
                    {
                        "name": "second",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "x", "source": "steps.fire.anything"}
                            ],
                        },
                    },
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown output"):
        CarrierDefinition.model_validate(bad)


def _book_with_label(label: dict[str, object], extract_name: str) -> dict[str, object]:
    return {
        "carrier": "dachser",
        "name": "Dachser",
        "auth": {"scheme": "none"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "labels",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.labels_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "order", "source": "shipment.order_number"}
                            ],
                        },
                        "response": {
                            "format": "json",
                            "extract": [{"name": extract_name, "path": "label.pdf"}],
                        },
                    }
                ],
                "label": label,
            }
        },
    }


def test_a_base64_pdf_label_with_a_valid_from_extract_validates() -> None:
    definition = CarrierDefinition.model_validate(
        _book_with_label(
            {"source": "base64_pdf", "from_extract": "label_pdf"}, "label_pdf"
        )
    )

    assert definition.operations["book"].label is not None


def test_a_base64_pdf_label_needs_a_from_extract() -> None:
    with pytest.raises(ValidationError, match="from_extract"):
        CarrierDefinition.model_validate(
            _book_with_label({"source": "base64_pdf"}, "label_pdf")
        )


def test_a_base64_pdf_label_from_extract_must_name_an_extraction() -> None:
    with pytest.raises(ValidationError, match="not an extraction"):
        CarrierDefinition.model_validate(
            _book_with_label(
                {"source": "base64_pdf", "from_extract": "nonexistent"}, "label_pdf"
            )
        )


def _book_with_allocate(
    allocate: list[dict[str, object]], op_name: str = "book"
) -> dict[str, object]:
    return {
        "carrier": "dachser",
        "name": "Dachser",
        "auth": {"scheme": "none"},
        "operations": {
            op_name: {
                "steps": [
                    {
                        "name": "labels",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.labels_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "o", "source": "shipment.order_number"}
                            ],
                        },
                    }
                ],
                "allocate": allocate,
            }
        },
    }


def test_a_book_allocate_sscc_block_validates() -> None:
    definition = CarrierDefinition.model_validate(
        _book_with_allocate(
            [
                {
                    "kind": "sscc",
                    "per": "parcel",
                    "prefix": "config.sscc_prefix",
                    "policy": "halt",
                }
            ]
        )
    )

    spec = definition.operations["book"].allocate[0]
    assert spec.kind == "sscc" and spec.prefix == "config.sscc_prefix"


def test_allocate_is_only_for_the_book_operation() -> None:
    with pytest.raises(ValidationError, match="allocate is only for the book"):
        CarrierDefinition.model_validate(
            _book_with_allocate(
                [{"kind": "sscc", "per": "parcel", "prefix": "config.sscc_prefix"}],
                op_name="track",
            )
        )


def test_an_allocate_prefix_must_be_a_config_source() -> None:
    with pytest.raises(ValidationError, match="must be a config"):
        CarrierDefinition.model_validate(
            _book_with_allocate(
                [{"kind": "sscc", "per": "parcel", "prefix": "shipment.order_number"}]
            )
        )


def test_an_sscc_allocation_may_not_wrap() -> None:
    # A wrapping SSCC would reissue a live code; only halt is admissible.
    with pytest.raises(ValidationError, match="must use policy 'halt'"):
        CarrierDefinition.model_validate(
            _book_with_allocate(
                [
                    {
                        "kind": "sscc",
                        "per": "parcel",
                        "prefix": "config.sscc_prefix",
                        "policy": "wrap",
                    }
                ]
            )
        )


def test_at_most_one_allocate_entry() -> None:
    # A parcel carries one carrier barcode, so a second spec could only
    # overwrite the first while spending its range.
    with pytest.raises(ValidationError, match="at most one allocate entry"):
        CarrierDefinition.model_validate(
            _book_with_allocate(
                [
                    {"kind": "sscc", "per": "parcel", "prefix": "config.sscc_prefix"},
                    {"kind": "sscc", "per": "parcel", "prefix": "config.other_prefix"},
                ]
            )
        )


def _manifest_operation(
    mapping: list[dict[str, object]], fan_out: bool = False
) -> dict[str, object]:
    # A fan-out manifest must use an upload transport; a batch manifest uses
    # http here.
    step: dict[str, object] = (
        {
            "name": "declare",
            "transport": "sftp_upload",
            "request": {
                "url": "config.sftp_remote_dir",
                "filename": "{shipment.order_number}.csv",
                "content_type": "csv",
                "mapping": mapping,
            },
        }
        if fan_out
        else {
            "name": "declare",
            "transport": "http",
            "request": {
                "method": "POST",
                "url": "config.manifest_url",
                "content_type": "json",
                "mapping": mapping,
            },
        }
    )
    operation: dict[str, object] = {"steps": [step]}
    if fan_out:
        operation["fan_out"] = True
    return {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {"scheme": "query_key", "param": "key", "secret": "config.api_key"},
        "operations": {"manifest": operation},
    }


def test_a_manifest_operation_sources_manifest_facts() -> None:
    definition = CarrierDefinition.model_validate(
        _manifest_operation(
            [
                {"target": "date", "source": "manifest.date"},
                {
                    "target": "orders",
                    "source": "manifest.consignments",
                    "each": [{"target": "order", "source": "item.order_number"}],
                },
                {"target": "account", "source": "config.account_number"},
                {"target": "depot", "source": "warehouse.code"},
            ]
        )
    )

    assert "manifest" in definition.operations


def test_a_fan_out_manifest_sources_shipment_facts() -> None:
    # A fan-out manifest renders once per consignment from that consignment's
    # own shipment.* facts, so it validates against shipment roots, not the
    # batch manifest.* roots.
    definition = CarrierDefinition.model_validate(
        _manifest_operation(
            [
                {"target": "order", "source": "shipment.order_number"},
                {"target": "depot", "source": "warehouse.code"},
                {"target": "account", "source": "config.account_number"},
            ],
            fan_out=True,
        )
    )

    assert definition.operations["manifest"].fan_out is True


def test_a_fan_out_manifest_rejects_manifest_facts() -> None:
    # manifest.* is the batch declaration; a fan-out manifest has no batch to
    # source from, so a manifest.* source must fail at authoring.
    bad = _manifest_operation(
        [{"target": "date", "source": "manifest.date"}], fan_out=True
    )

    with pytest.raises(ValidationError, match="unknown source root 'manifest'"):
        CarrierDefinition.model_validate(bad)


def test_fan_out_is_only_valid_on_the_manifest_operation() -> None:
    bad = {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {"scheme": "query_key", "param": "key", "secret": "config.api_key"},
        "operations": {
            "book": {
                "fan_out": True,
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "o", "source": "shipment.order_number"}
                            ],
                        },
                    }
                ],
                "label": {"source": "local_render"},
            }
        },
    }

    with pytest.raises(ValidationError, match="fan_out"):
        CarrierDefinition.model_validate(bad)


def test_a_fan_out_manifest_must_use_an_upload_transport() -> None:
    # Whole-manifest retry re-sends every document, safe only for
    # overwrite-idempotent uploads, so a fan_out manifest over http is refused.
    bad = {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {"scheme": "none"},
        "operations": {
            "manifest": {
                "fan_out": True,
                "steps": [
                    {
                        "name": "declare",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.manifest_url",
                            "content_type": "json",
                            "mapping": [
                                {"target": "o", "source": "shipment.order_number"}
                            ],
                        },
                    }
                ],
            }
        },
    }

    with pytest.raises(ValidationError, match="upload transport"):
        CarrierDefinition.model_validate(bad)


def test_manifest_facts_are_rejected_outside_the_manifest_operation() -> None:
    bad = {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {"scheme": "query_key", "param": "key", "secret": "config.api_key"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [{"target": "date", "source": "manifest.date"}],
                        },
                    }
                ],
                "label": {"source": "local_render"},
            }
        },
    }

    with pytest.raises(ValidationError, match="unknown source root 'manifest'"):
        CarrierDefinition.model_validate(bad)


def test_shipment_facts_are_rejected_inside_the_manifest_operation() -> None:
    # A manifest declares many consignments; there is no single shipment to
    # source from - individual consignments are item.* inside an each-loop.
    bad = _manifest_operation([{"target": "order", "source": "shipment.order_number"}])

    with pytest.raises(ValidationError, match="unknown source root 'shipment'"):
        CarrierDefinition.model_validate(bad)


def test_auth_secret_must_resolve_in_the_manifest_context_too() -> None:
    """One auth block serves every operation. A definition that manifests
    cannot authenticate from shipment.* facts (a manifest has no single
    shipment), so a shipment-sourced secret must fail at authoring, not at
    trailer-close on the worker."""
    bad = {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {
            "scheme": "header_key",
            "header": "X-Api-Key",
            "secret": "shipment.order_number",
        },
        "operations": {
            "manifest": {
                "steps": [
                    {
                        "name": "declare",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.manifest_url",
                            "content_type": "json",
                            "mapping": [{"target": "date", "source": "manifest.date"}],
                        },
                    }
                ]
            },
        },
    }

    with pytest.raises(ValidationError, match="unknown source root 'shipment'"):
        CarrierDefinition.model_validate(bad)


def test_config_auth_secret_serves_both_book_and_manifest_operations() -> None:
    # config.* is the credential home (CONTEXT.md: Carrier Config) and
    # resolves in every operation context, so it validates even when the
    # definition carries both a book and a manifest operation.
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "furdeco",
            "name": "Furdeco",
            "auth": {
                "scheme": "query_key",
                "param": "key",
                "secret": "config.api_key",
            },
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "save",
                            "transport": "http",
                            "request": {
                                "method": "POST",
                                "url": "config.base_url",
                                "content_type": "form",
                                "mapping": [
                                    {
                                        "target": "order",
                                        "source": "shipment.order_number",
                                    }
                                ],
                            },
                        }
                    ],
                    "label": {"source": "local_render"},
                },
                "manifest": {
                    "steps": [
                        {
                            "name": "declare",
                            "transport": "http",
                            "request": {
                                "method": "POST",
                                "url": "config.manifest_url",
                                "content_type": "json",
                                "mapping": [
                                    {"target": "date", "source": "manifest.date"}
                                ],
                            },
                        }
                    ]
                },
            },
        }
    )

    assert set(definition.operations) == {"book", "manifest"}


def _ftp_step(**overrides: object) -> dict[str, object]:
    request: dict[str, object] = {
        "url": "config.ftp_remote_dir",
        "filename": "{shipment.order_number}.csv",
        "content_type": "csv",
        "mapping": [{"target": "order", "source": "shipment.order_number"}],
    }
    request.update(overrides)
    return {
        "carrier": "fagans",
        "name": "Fagans",
        "auth": {"scheme": "none"},
        "operations": {
            "book": {
                "steps": [
                    {"name": "upload", "transport": "ftp_upload", "request": request}
                ],
                "label": {"source": "local_render"},
            }
        },
    }


def test_a_valid_ftp_upload_step_validates() -> None:
    definition = CarrierDefinition.model_validate(_ftp_step())

    step = definition.operations["book"].steps[0]
    assert step.transport == "ftp_upload"
    assert step.request.filename == "{shipment.order_number}.csv"


def test_an_upload_step_requires_a_filename() -> None:
    with pytest.raises(ValidationError, match="filename"):
        CarrierDefinition.model_validate(_ftp_step(filename=None))


def test_an_upload_step_must_be_csv() -> None:
    with pytest.raises(ValidationError, match="csv"):
        CarrierDefinition.model_validate(_ftp_step(content_type="json"))


def test_an_upload_step_takes_no_response() -> None:
    # Uploads are fire-and-forget: declaring a response/extraction would
    # promise data that never comes back.
    bad = _ftp_step()
    bad["operations"]["book"]["steps"][0]["response"] = {  # type: ignore[index]
        "format": "json",
        "extract": [{"name": "x", "path": "y"}],
    }
    with pytest.raises(ValidationError, match="response"):
        CarrierDefinition.model_validate(bad)


def test_an_http_step_may_not_carry_a_filename() -> None:
    bad = {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {"scheme": "none"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "filename": "{shipment.order_number}.csv",
                            "content_type": "json",
                            "mapping": [
                                {"target": "o", "source": "shipment.order_number"}
                            ],
                        },
                    }
                ],
                "label": {"source": "local_render"},
            }
        },
    }
    with pytest.raises(ValidationError, match="filename"):
        CarrierDefinition.model_validate(bad)


def test_a_filename_placeholder_must_resolve_to_a_known_fact() -> None:
    with pytest.raises(ValidationError, match="filename"):
        CarrierDefinition.model_validate(
            _ftp_step(filename="{shipment.nope}-{bogus.thing}.csv")
        )


def test_an_upload_remote_directory_must_come_from_config() -> None:
    # The remote directory is per-install config, never shipment data - a
    # shipment-sourced value could carry `..` and escape the directory.
    with pytest.raises(ValidationError, match="config"):
        CarrierDefinition.model_validate(_ftp_step(url="shipment.order_number"))


def test_a_valid_xml_upload_step_validates() -> None:
    definition = CarrierDefinition.model_validate(
        _ftp_step(
            content_type="xml",
            root_element="ForwardingOrderInformation",
            filename="{shipment.order_number}.xml",
            mapping=[
                {"target": "@Version", "const": "2.0"},
                {"target": "Order.Number", "source": "shipment.order_number"},
                {
                    "target": "ShipmentLine",
                    "source": "shipment.parcels",
                    "each": [{"target": "@Sequence", "source": "item.seq"}],
                },
            ],
        )
    )

    step = definition.operations["book"].steps[0]
    assert step.request.content_type == "xml"
    assert step.request.root_element == "ForwardingOrderInformation"


def test_an_xml_upload_step_needs_a_root_element() -> None:
    with pytest.raises(ValidationError, match="root_element"):
        CarrierDefinition.model_validate(_ftp_step(content_type="xml"))


def test_root_element_is_only_for_xml() -> None:
    with pytest.raises(ValidationError, match="root_element is only for content_type"):
        CarrierDefinition.model_validate(_ftp_step(root_element="Order"))


def test_an_xml_attribute_must_be_the_terminal_segment() -> None:
    with pytest.raises(ValidationError, match="last segment"):
        CarrierDefinition.model_validate(
            _ftp_step(
                content_type="xml",
                root_element="Order",
                mapping=[{"target": "@Bad.Inner", "source": "shipment.order_number"}],
            )
        )


def test_an_xml_attribute_needs_a_name_after_the_at_sign() -> None:
    # A bare "@" would strip to an empty attribute name and render malformed
    # XML (`<Root ="v"/>`), so it is refused at authoring.
    with pytest.raises(ValidationError, match="needs a name"):
        CarrierDefinition.model_validate(
            _ftp_step(
                content_type="xml",
                root_element="Root",
                mapping=[{"target": "@", "const": "v"}],
            )
        )


def test_an_xml_element_name_must_be_a_legal_xml_name() -> None:
    # A target segment with a space (or an @ mid-name, etc.) would render a
    # tag no XML parser can read; it is refused at authoring.
    with pytest.raises(ValidationError, match="legal XML name"):
        CarrierDefinition.model_validate(
            _ftp_step(
                content_type="xml",
                root_element="Order",
                mapping=[{"target": "Ship To", "source": "shipment.order_number"}],
            )
        )


def test_an_xml_root_element_must_be_a_legal_xml_name() -> None:
    with pytest.raises(ValidationError, match="legal XML name"):
        CarrierDefinition.model_validate(
            _ftp_step(content_type="xml", root_element="Bad Root")
        )


def test_an_xml_xmlns_attribute_is_rejected() -> None:
    # xmlns needs no colon, so the no-namespace-colon rule misses it; declaring
    # a default namespace would silently put the whole document in it.
    with pytest.raises(ValidationError, match="xmlns"):
        CarrierDefinition.model_validate(
            _ftp_step(
                content_type="xml",
                root_element="Order",
                mapping=[{"target": "@xmlns", "const": "http://example.com/ns"}],
            )
        )


def test_an_xml_attribute_cannot_be_a_repeated_element() -> None:
    with pytest.raises(ValidationError, match="repeated element"):
        CarrierDefinition.model_validate(
            _ftp_step(
                content_type="xml",
                root_element="Order",
                mapping=[
                    {
                        "target": "@Lines",
                        "source": "shipment.parcels",
                        "each": [{"target": "W", "source": "item.weight_kg"}],
                    }
                ],
            )
        )


def test_xml_is_an_upload_only_content_type() -> None:
    # There is no http-xml request body: an http step declaring xml is refused
    # (root_element set so the failure is the upload-only rule, not a missing
    # root_element).
    bad = {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {"scheme": "none"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "xml",
                            "root_element": "Order",
                            "mapping": [
                                {"target": "o", "source": "shipment.order_number"}
                            ],
                        },
                    }
                ],
                "label": {"source": "local_render"},
            }
        },
    }
    with pytest.raises(ValidationError, match="upload-only"):
        CarrierDefinition.model_validate(bad)
