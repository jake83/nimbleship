"""The engine's pure renderer: (definition, operation, facts) -> rendered
requests. Pure by design - Golden Replay diffs renders without touching a
carrier (ADR 0009)."""

import pytest

from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.engine.field_plugins import FIELD_PLUGINS
from nimbleship.engine.render import render_operation

DEFINITION = CarrierDefinition.model_validate(
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
                            "query": {"action": "save"},
                            "content_type": "form",
                            "mapping": [
                                {
                                    "target": "order_number",
                                    "source": "shipment.order_number",
                                },
                                {
                                    "target": "postcode",
                                    "source": "shipment.postcode",
                                    "transform": {"name": "uppercase"},
                                },
                                {
                                    "target": "address",
                                    "source": "shipment.address_lines",
                                    "transform": {"name": "join", "with": ", "},
                                },
                                {
                                    "target": "delivery_point",
                                    "source": "shipment.two_man",
                                    "transform": {
                                        "name": "lookup",
                                        "table": {
                                            "true": "Room Of Choice",
                                            "false": "Hallway",
                                        },
                                    },
                                },
                                {"target": "service_level", "const": "2 Man"},
                                {
                                    "target": "items",
                                    "source": "shipment.parcels",
                                    "each": [
                                        {
                                            "target": "weight_kg",
                                            "source": "item.weight_kg",
                                        }
                                    ],
                                },
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
            }
        },
    }
)

FACTS = {
    "shipment": {
        "order_number": "95000254580",
        "postcode": "iv1 2ab",
        "address_lines": ["10 Downing Street", "London"],
        "two_man": True,
        "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],
    },
    "config": {
        "api_key": "SECRET-KEY",
        "base_url": "https://api.furdeco.example/orders",
    },
}


def test_renders_mapped_transformed_and_constant_fields() -> None:
    [request] = render_operation(DEFINITION, "book", FACTS)

    assert request.step == "save"
    assert request.method == "POST"
    assert request.url == "https://api.furdeco.example/orders"
    assert request.body["order_number"] == "95000254580"
    assert request.body["postcode"] == "IV1 2AB"
    assert request.body["address"] == "10 Downing Street, London"
    assert request.body["delivery_point"] == "Room Of Choice"
    assert request.body["service_level"] == "2 Man"


def test_renders_each_loops_over_collections() -> None:
    [request] = render_operation(DEFINITION, "book", FACTS)

    assert request.body["items"] == [
        {"weight_kg": "4.2"},
        {"weight_kg": "3.1"},
    ]


def test_auth_query_key_lands_in_query_not_body() -> None:
    [request] = render_operation(DEFINITION, "book", FACTS)

    assert request.query == {"action": "save", "key": "SECRET-KEY"}
    assert "key" not in request.body


def test_missing_fact_fails_loudly_with_the_path_named() -> None:
    facts = {**FACTS, "shipment": {"order_number": "X"}}

    with pytest.raises(ValueError, match=r"shipment\.postcode"):
        render_operation(DEFINITION, "book", facts)


def test_renders_are_deterministic() -> None:
    first = render_operation(DEFINITION, "book", FACTS)
    second = render_operation(DEFINITION, "book", FACTS)

    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]


def test_each_over_an_unresolved_step_output_renders_a_placeholder() -> None:
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "palletforce",
            "name": "PalletForce",
            "auth": {"scheme": "none"},
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
                                "extract": [{"name": "labels", "path": "labelImages"}],
                            },
                        },
                        {
                            "name": "fetch",
                            "transport": "http",
                            "request": {
                                "method": "POST",
                                "url": "config.base_url",
                                "content_type": "json",
                                "mapping": [
                                    {
                                        "target": "images",
                                        "source": "steps.manifest.labels",
                                        "each": [
                                            {
                                                "target": "data",
                                                "source": "item.imageData",
                                            }
                                        ],
                                    }
                                ],
                            },
                        },
                    ],
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.pf.example"},
    }

    _, second = render_operation(definition, "book", facts)

    assert second.body["images"] == "<steps.manifest.labels>"
    # Deterministic across renders - the placeholder is stable.
    assert render_operation(definition, "book", facts)[1].body["images"] == (
        "<steps.manifest.labels>"
    )


def test_header_key_auth_is_injected_into_headers() -> None:
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "dachser",
            "name": "Dachser",
            "auth": {
                "scheme": "header_key",
                "header": "X-API-Key",
                "secret": "config.client_id",
            },
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "labels",
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
                        }
                    ],
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.d.example", "client_id": "K-1"},
    }

    [request] = render_operation(definition, "book", facts)

    assert request.headers == {"X-API-Key": "K-1"}
    assert "X-API-Key" not in request.body


def test_transform_over_an_unresolved_step_output_keeps_the_placeholder() -> None:
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "x",
            "name": "X",
            "auth": {"scheme": "none"},
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "first",
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
                                "extract": [{"name": "ref", "path": "ref"}],
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
                                    {
                                        "target": "ref_upper",
                                        "source": "steps.first.ref",
                                        "transform": {"name": "uppercase"},
                                    }
                                ],
                            },
                        },
                    ],
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.x.example"},
    }

    _, second = render_operation(definition, "book", facts)

    assert second.body["ref_upper"] == "<steps.first.ref>"


def test_a_real_value_shaped_like_a_placeholder_is_still_transformed() -> None:
    """Placeholders are a type, not a magic string: genuine data that
    happens to look like a token must not silently skip its transforms."""
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "x",
            "name": "X",
            "auth": {"scheme": "none"},
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
                                    {
                                        "target": "ref",
                                        "source": "shipment.reference",
                                        "transform": {"name": "uppercase"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"reference": "<steps.only.ref>"},
        "config": {"base_url": "https://api.x.example"},
    }

    [request] = render_operation(definition, "book", facts)

    assert request.body["ref"] == "<STEPS.ONLY.REF>"


class _EchoOrderPlugin:
    """Computes a value from the facts it is handed - proof the renderer
    passes the whole facts dict through."""

    def compute(self, facts: dict[str, object]) -> object:
        shipment = facts["shipment"]
        assert isinstance(shipment, dict)
        return f"computed:{shipment['order_number']}"


def _single_entry_definition(entry: dict[str, object]) -> CarrierDefinition:
    return _single_step_definition([entry])


def _single_step_definition(mapping: list[dict[str, object]]) -> CarrierDefinition:
    return CarrierDefinition.model_validate(
        {
            "carrier": "x",
            "name": "X",
            "auth": {"scheme": "none"},
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
                                "mapping": mapping,
                            },
                        }
                    ],
                }
            },
        }
    )


def test_plugin_entries_render_by_calling_the_registered_plugin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(FIELD_PLUGINS, "test_echo_order", _EchoOrderPlugin())
    definition = _single_entry_definition(
        {"target": "reference", "plugin": "test_echo_order"}
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"base_url": "https://api.x.example"},
    }

    [request] = render_operation(definition, "book", facts)

    assert request.body["reference"] == "computed:95000254580"


def _entries_definition(*entries: dict[str, object]) -> CarrierDefinition:
    return CarrierDefinition.model_validate(
        {
            "carrier": "x",
            "name": "X",
            "auth": {"scheme": "none"},
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
                                "mapping": list(entries),
                            },
                        }
                    ],
                }
            },
        }
    )


def test_dotted_targets_render_nested_objects_and_lists() -> None:
    definition = _entries_definition(
        {"target": "deliveryAddress.name", "source": "shipment.recipient_name"},
        {"target": "deliveryAddress.postcode", "source": "shipment.postcode"},
        {"target": "consignments.0.weight", "source": "shipment.weight_kg"},
        {"target": "consignments.0.pallets.0.palletType", "const": "H"},
        {"target": "consignments.0.pallets.0.numberofPallets", "const": "1"},
    )
    facts: dict[str, object] = {
        "shipment": {
            "recipient_name": "John Doe",
            "postcode": "IV1 2AB",
            "weight_kg": "410",
        },
        "config": {"base_url": "https://api.x.example"},
    }

    [request] = render_operation(definition, "book", facts)

    assert request.body == {
        "deliveryAddress": {"name": "John Doe", "postcode": "IV1 2AB"},
        "consignments": [
            {
                "weight": "410",
                "pallets": [{"palletType": "H", "numberofPallets": "1"}],
            }
        ],
    }


def test_source_paths_index_into_extracted_lists() -> None:
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "x",
            "name": "X",
            "auth": {"scheme": "none"},
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
                                    {"target": "order", "source": "shipment.order"}
                                ],
                            },
                            "response": {
                                "format": "json",
                                "extract": [
                                    {
                                        "name": "tracking_codes",
                                        "path": "successfulTrackingCodes",
                                    }
                                ],
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
                                        "source": "steps.manifest.tracking_codes.0",
                                    }
                                ],
                            },
                        },
                    ],
                }
            },
        }
    )
    base_facts: dict[str, object] = {
        "shipment": {"order": "95000254580"},
        "config": {"base_url": "https://api.x.example"},
    }

    # Before the manifest step has answered: a stable placeholder.
    _, before = render_operation(definition, "book", base_facts)
    assert before.body["trackingNumber"] == "<steps.manifest.tracking_codes.0>"

    # After execution injects the extracted outputs: the first code.
    facts = {
        **base_facts,
        "steps": {"manifest": {"tracking_codes": ["UMB0000042", "UMB0000043"]}},
    }
    _, after = render_operation(definition, "book", facts)
    assert after.body["trackingNumber"] == "UMB0000042"


NESTING_FACTS: dict[str, object] = {
    "shipment": {
        "order_number": "95000254580",
        "recipient_name": "John Doe",
        "parcels": [{"weight_kg": "4.20"}, {"weight_kg": "3.10"}],
    },
    "config": {"base_url": "https://api.x.example", "account_number": "802255209"},
}


def test_dotted_targets_render_as_nested_objects() -> None:
    definition = _single_step_definition(
        [
            {"target": "accountNumber.value", "source": "config.account_number"},
            {"target": "requestedShipment.pickupType", "const": "USE_SCHEDULED_PICKUP"},
            {
                "target": "requestedShipment.labelSpecification.imageType",
                "const": "PNG",
            },
        ]
    )

    [request] = render_operation(definition, "book", NESTING_FACTS)

    assert request.body == {
        "accountNumber": {"value": "802255209"},
        "requestedShipment": {
            "pickupType": "USE_SCHEDULED_PICKUP",
            "labelSpecification": {"imageType": "PNG"},
        },
    }


def test_numeric_target_segments_render_as_list_positions() -> None:
    definition = _single_step_definition(
        [
            {
                "target": "recipients.0.contact.personName",
                "source": "shipment.recipient_name",
            },
            {"target": "recipients.0.address.streetLines.0", "const": "10 High St"},
        ]
    )

    [request] = render_operation(definition, "book", NESTING_FACTS)

    assert request.body == {
        "recipients": [
            {
                "contact": {"personName": "John Doe"},
                "address": {"streetLines": ["10 High St"]},
            }
        ]
    }


def test_dotted_targets_nest_inside_each_loops() -> None:
    definition = _single_step_definition(
        [
            {
                "target": "requestedPackageLineItems",
                "source": "shipment.parcels",
                "each": [
                    {"target": "weight.units", "const": "KG"},
                    {"target": "weight.value", "source": "item.weight_kg"},
                ],
            }
        ]
    )

    [request] = render_operation(definition, "book", NESTING_FACTS)

    assert request.body == {
        "requestedPackageLineItems": [
            {"weight": {"units": "KG", "value": "4.20"}},
            {"weight": {"units": "KG", "value": "3.10"}},
        ]
    }


def test_a_target_colliding_with_a_nested_value_fails_at_authoring() -> None:
    """Target collisions die at draft time now (the schema dry-runs its
    targets), which is strictly earlier than the render-time failure this
    test originally pinned."""
    import pytest

    with pytest.raises(ValueError, match="accountNumber"):
        _single_step_definition(
            [
                {
                    "target": "accountNumber.value",
                    "source": "config.account_number",
                },
                {"target": "accountNumber", "source": "shipment.order_number"},
            ]
        )


def test_a_list_index_that_skips_a_position_fails_at_authoring() -> None:
    """Skipped list positions die at draft time (the schema dry-runs its
    targets) - strictly earlier than the render-time failure originally
    pinned here."""
    import pytest

    with pytest.raises(ValueError, match=r"recipients\.1\.name"):
        _single_step_definition(
            [{"target": "recipients.1.name", "source": "shipment.recipient_name"}]
        )


def test_auth_is_not_injected_into_non_http_steps() -> None:
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "fagans",
            "name": "Fagans",
            "auth": {
                "scheme": "header_key",
                "header": "X-Key",
                "secret": "config.api_key",
            },
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "csv",
                            "transport": "ftp_upload",
                            "request": {
                                "method": "POST",
                                "url": "config.ftp_path",
                                "content_type": "csv",
                                "mapping": [
                                    {
                                        "target": "order",
                                        "source": "shipment.order_number",
                                    }
                                ],
                            },
                        }
                    ],
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"api_key": "SECRET", "ftp_path": "/outbound"},
    }

    [request] = render_operation(definition, "book", facts)

    assert request.headers == {}
    assert "SECRET" not in repr(request.model_dump())
