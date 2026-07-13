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


def test_plugin_entries_take_no_transform_or_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(FIELD_PLUGINS, "test_static", _StaticPlugin())
    for extra in (
        {"transform": {"name": "uppercase"}},
        {"each": [{"target": "y", "source": "item.value"}]},
    ):
        entry: dict[str, object] = {"target": "x", "plugin": "test_static", **extra}
        with pytest.raises(ValidationError, match="no transform or each"):
            CarrierDefinition.model_validate(_with_entries(entry))


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


def _manifest_operation(mapping: list[dict[str, object]]) -> dict[str, object]:
    return {
        "carrier": "furdeco",
        "name": "Furdeco",
        "auth": {"scheme": "query_key", "param": "key", "secret": "config.api_key"},
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
                            "mapping": mapping,
                        },
                    }
                ]
            }
        },
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
