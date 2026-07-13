"""The engine's pure renderer: (definition, operation, facts) -> rendered
requests. Pure by design - Golden Replay diffs renders without touching a
carrier (ADR 0009)."""

import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.engine.field_plugins import FIELD_PLUGINS
from nimbleship.engine.render import RenderedUpload, render_operation
from render_support import http_renders

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


UPLOAD_DEFINITION = CarrierDefinition.model_validate(
    {
        "carrier": "fagans",
        "name": "Fagans",
        "auth": {"scheme": "none"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "upload",
                        "transport": "ftp_upload",
                        "request": {
                            "url": "config.ftp_remote_dir",
                            "filename": "{warehouse.code}-{shipment.order_number}.csv",
                            "content_type": "csv",
                            "mapping": [
                                {"target": "account", "source": "config.account_code"},
                                {
                                    "target": "load_number",
                                    "source": "shipment.order_number",
                                    "transform": {
                                        "name": "format",
                                        "template": "DMC{}",
                                    },
                                },
                                {
                                    "target": "recipient",
                                    "source": "shipment.recipient_name",
                                },
                                {
                                    "target": "address",
                                    "source": "shipment.address_lines",
                                    "transform": {"name": "join", "with": ", "},
                                },
                            ],
                        },
                    }
                ],
                "label": {"source": "local_render", "template": "standard_a6"},
            }
        },
    }
)

UPLOAD_FACTS = {
    "shipment": {
        "order_number": "95000254580",
        "recipient_name": "John Doe",
        "address_lines": ["10 Downing Street", "London"],
    },
    "warehouse": {"code": "L2"},
    "config": {"account_code": "LIM2", "ftp_remote_dir": "/outbound"},
}


def test_an_upload_step_renders_to_a_file_not_an_http_request() -> None:
    [rendered] = render_operation(UPLOAD_DEFINITION, "book", UPLOAD_FACTS)

    assert isinstance(rendered, RenderedUpload)
    assert rendered.step == "upload"
    assert rendered.transport == "ftp_upload"
    assert rendered.content_type == "csv"
    # The url source resolves to the remote directory; the filename template
    # substitutes facts (and the format transform built the DMC reference).
    assert rendered.remote_path == "/outbound"
    assert rendered.filename == "L2-95000254580.csv"


def test_csv_content_is_one_ordered_row_rfc4180_quoted() -> None:
    [rendered] = render_operation(UPLOAD_DEFINITION, "book", UPLOAD_FACTS)

    assert isinstance(rendered, RenderedUpload)
    # Ordered by the mapping; a field containing the delimiter is quoted;
    # a trailing CRLF per RFC 4180 (what the legacy fputcsv produced).
    assert rendered.content == (
        'LIM2,DMC95000254580,John Doe,"10 Downing Street, London"\r\n'
    )


def test_upload_render_is_deterministic_and_secret_free() -> None:
    first = render_operation(UPLOAD_DEFINITION, "book", UPLOAD_FACTS)
    second = render_operation(UPLOAD_DEFINITION, "book", UPLOAD_FACTS)
    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]
    # Connection secrets are used by the uploader at execution, never
    # rendered - nothing here should carry a host/username/password.
    dumped = str(first[0].model_dump())
    assert "password" not in dumped and "ftp_host" not in dumped


XML_DEFINITION = CarrierDefinition.model_validate(
    {
        "carrier": "dachser",
        "name": "Dachser",
        "auth": {"scheme": "none"},
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "edi",
                        "transport": "sftp_upload",
                        "request": {
                            "url": "config.sftp_remote_dir",
                            "filename": "{shipment.order_number}.xml",
                            "content_type": "xml",
                            "root_element": "ForwardingOrderInformation",
                            "mapping": [
                                {"target": "@Version", "const": "2.0"},
                                {
                                    "target": "Order.OrderNumber",
                                    "source": "shipment.order_number",
                                },
                                {
                                    "target": "ShipmentAddress.@AddressType",
                                    "const": "Consignee",
                                },
                                {
                                    "target": "ShipmentAddress.@Country",
                                    "const": "CZ",
                                },
                                {
                                    "target": "ShipmentAddress.City",
                                    "source": "shipment.city",
                                },
                                {
                                    "target": "ShipmentLine",
                                    "source": "shipment.parcels",
                                    "each": [
                                        {"target": "@Sequence", "source": "item.seq"},
                                        {
                                            "target": "Weight",
                                            "source": "item.weight_kg",
                                        },
                                    ],
                                },
                            ],
                        },
                    }
                ],
                "label": {"source": "local_render", "template": "standard_a6"},
            }
        },
    }
)

XML_FACTS = {
    "shipment": {
        "order_number": "95000254580",
        "city": "Praha",
        "parcels": [
            {"seq": "1", "weight_kg": "4.2"},
            {"seq": "2", "weight_kg": "3.1"},
        ],
    },
    "config": {"sftp_remote_dir": "/inbox"},
}


def test_xml_upload_renders_prolog_root_attributes_nesting_and_repeats() -> None:
    [rendered] = render_operation(XML_DEFINITION, "book", XML_FACTS)

    assert isinstance(rendered, RenderedUpload)
    assert rendered.content_type == "xml"
    assert rendered.filename == "95000254580.xml"
    # A fixed UTF-8 prolog; @-targets become attributes of their element
    # (@Version on the root, @AddressType/@Country/@Sequence on their parents);
    # two attributes on one element keep mapping order; dot targets nest
    # elements; an each-loop becomes repeated same-name elements.
    assert rendered.content == (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<ForwardingOrderInformation Version="2.0">'
        "<Order><OrderNumber>95000254580</OrderNumber></Order>"
        '<ShipmentAddress AddressType="Consignee" Country="CZ">'
        "<City>Praha</City></ShipmentAddress>"
        '<ShipmentLine Sequence="1"><Weight>4.2</Weight></ShipmentLine>'
        '<ShipmentLine Sequence="2"><Weight>3.1</Weight></ShipmentLine>'
        "</ForwardingOrderInformation>"
    )
    # And the document is well-formed - it round-trips through a parser.
    ET.fromstring(rendered.content)


def test_xml_refuses_a_control_character_in_a_rendered_value() -> None:
    # A control character XML 1.0 forbids (here a vertical tab) can arrive in
    # ordinary free-text shipment data; it must fail loudly at render, never
    # ship as a broken EDI file no parser can read.
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "dachser",
            "name": "Dachser",
            "auth": {"scheme": "none"},
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "edi",
                            "transport": "sftp_upload",
                            "request": {
                                "url": "config.sftp_remote_dir",
                                "filename": "{shipment.order_number}.xml",
                                "content_type": "xml",
                                "root_element": "Order",
                                "mapping": [
                                    {"target": "Notes", "source": "shipment.notes"}
                                ],
                            },
                        }
                    ],
                    "label": {"source": "local_render", "template": "a6"},
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "1", "notes": "line1\x0bline2"},
        "config": {"sftp_remote_dir": "/inbox"},
    }

    with pytest.raises(ValueError, match="control character"):
        render_operation(definition, "book", facts)


def test_xml_escapes_special_characters_in_text_and_attributes() -> None:
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "dachser",
            "name": "Dachser",
            "auth": {"scheme": "none"},
            "operations": {
                "book": {
                    "steps": [
                        {
                            "name": "edi",
                            "transport": "sftp_upload",
                            "request": {
                                "url": "config.sftp_remote_dir",
                                "filename": "{shipment.order_number}.xml",
                                "content_type": "xml",
                                "root_element": "Order",
                                "mapping": [
                                    {"target": "@Ref", "source": "shipment.ref"},
                                    {"target": "Notes", "source": "shipment.notes"},
                                ],
                            },
                        }
                    ],
                    "label": {"source": "local_render", "template": "a6"},
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "1", "ref": 'A&B"C', "notes": "x < y & z"},
        "config": {"sftp_remote_dir": "/inbox"},
    }

    [rendered] = render_operation(definition, "book", facts)

    assert isinstance(rendered, RenderedUpload)
    # Markup metacharacters are escaped, so a value can never inject structure.
    assert '<Order Ref="A&amp;B&quot;C">' in rendered.content
    assert "<Notes>x &lt; y &amp; z</Notes>" in rendered.content


def test_xml_upload_render_is_deterministic_and_secret_free() -> None:
    first = render_operation(XML_DEFINITION, "book", XML_FACTS)
    second = render_operation(XML_DEFINITION, "book", XML_FACTS)
    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]
    # Connection secrets are read by the uploader at execution, never rendered -
    # nothing here should carry a host/username/password (as the csv path also
    # guarantees).
    dumped = str(first[0].model_dump())
    assert "password" not in dumped and "sftp_host" not in dumped


def test_renders_mapped_transformed_and_constant_fields() -> None:
    [request] = http_renders(DEFINITION, "book", FACTS)

    assert request.step == "save"
    assert request.method == "POST"
    assert request.url == "https://api.furdeco.example/orders"
    assert request.body["order_number"] == "95000254580"
    assert request.body["postcode"] == "IV1 2AB"
    assert request.body["address"] == "10 Downing Street, London"
    assert request.body["delivery_point"] == "Room Of Choice"
    assert request.body["service_level"] == "2 Man"


def test_format_transform_prefixes_a_scalar() -> None:
    # A `format` transform substitutes the value into a template's single
    # `{}` placeholder - the engine-owned way to build the DMC-prefixed
    # consignment reference (DMC + order number) a carrier like Fagans needs.
    definition = CarrierDefinition.model_validate(
        {
            "carrier": "fagans",
            "name": "Fagans",
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
                                "content_type": "form",
                                "mapping": [
                                    {
                                        "target": "consignment",
                                        "source": "shipment.order_number",
                                        "transform": {
                                            "name": "format",
                                            "template": "DMC{}",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "label": {"source": "local_render"},
                }
            },
        }
    )

    [request] = http_renders(
        definition,
        "book",
        {"config": {"base_url": "x"}, "shipment": {"order_number": "95000254580"}},
    )

    assert request.body["consignment"] == "DMC95000254580"


def test_format_transform_requires_a_single_placeholder() -> None:
    def _entry(template: str) -> dict[str, object]:
        return {
            "carrier": "fagans",
            "name": "Fagans",
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
                                "content_type": "form",
                                "mapping": [
                                    {
                                        "target": "ref",
                                        "source": "shipment.order_number",
                                        "transform": {
                                            "name": "format",
                                            "template": template,
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "label": {"source": "local_render"},
                }
            },
        }

    # No placeholder silently drops the fact; more than one is ambiguous; a
    # stray unbalanced brace would survive the substitution. All fail at
    # authoring.
    for bad in ("DMC", "{}{}", "}{}", "a}b{}", "{}x{"):
        with pytest.raises(ValidationError, match="placeholder"):
            CarrierDefinition.model_validate(_entry(bad))


def test_renders_each_loops_over_collections() -> None:
    [request] = http_renders(DEFINITION, "book", FACTS)

    assert request.body["items"] == [
        {"weight_kg": "4.2"},
        {"weight_kg": "3.1"},
    ]


def test_auth_query_key_lands_in_query_not_body() -> None:
    [request] = http_renders(DEFINITION, "book", FACTS)

    assert request.query == {"action": "save", "key": "SECRET-KEY"}
    assert "key" not in request.body


def test_missing_fact_fails_loudly_with_the_path_named() -> None:
    facts = {**FACTS, "shipment": {"order_number": "X"}}

    with pytest.raises(ValueError, match=r"shipment\.postcode"):
        http_renders(DEFINITION, "book", facts)


def test_renders_are_deterministic() -> None:
    first = http_renders(DEFINITION, "book", FACTS)
    second = http_renders(DEFINITION, "book", FACTS)

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

    _, second = http_renders(definition, "book", facts)

    assert second.body["images"] == "<steps.manifest.labels>"
    # Deterministic across renders - the placeholder is stable.
    assert http_renders(definition, "book", facts)[1].body["images"] == (
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

    [request] = http_renders(definition, "book", facts)

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

    _, second = http_renders(definition, "book", facts)

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

    [request] = http_renders(definition, "book", facts)

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

    [request] = http_renders(definition, "book", facts)

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

    [request] = http_renders(definition, "book", facts)

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
    _, before = http_renders(definition, "book", base_facts)
    assert before.body["trackingNumber"] == "<steps.manifest.tracking_codes.0>"

    # After execution injects the extracted outputs: the first code.
    facts = {
        **base_facts,
        "steps": {"manifest": {"tracking_codes": ["UMB0000042", "UMB0000043"]}},
    }
    _, after = http_renders(definition, "book", facts)
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

    [request] = http_renders(definition, "book", NESTING_FACTS)

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

    [request] = http_renders(definition, "book", NESTING_FACTS)

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

    [request] = http_renders(definition, "book", NESTING_FACTS)

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
                                "url": "config.ftp_path",
                                "filename": "{shipment.order_number}.csv",
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
                    "label": {"source": "local_render"},
                }
            },
        }
    )
    facts: dict[str, object] = {
        "shipment": {"order_number": "95000254580"},
        "config": {"api_key": "SECRET", "ftp_path": "/outbound"},
    }

    [rendered] = render_operation(definition, "book", facts)

    # An upload render structurally carries no auth channel (no headers or
    # query), and the auth secret is never resolved into it.
    assert isinstance(rendered, RenderedUpload)
    assert "SECRET" not in repr(rendered.model_dump())
