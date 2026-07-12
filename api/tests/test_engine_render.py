"""The engine's pure renderer: (definition, operation, facts) -> rendered
requests. Pure by design - Golden Replay diffs renders without touching a
carrier (ADR 0009)."""

from nimbleship.domain.carrier_definition import CarrierDefinition
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
    import pytest

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
