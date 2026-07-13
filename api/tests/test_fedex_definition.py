"""The FedEx example definition: plugin auth plus a single http step
posting the ship/v1 booking shape. It must validate against the Carrier
Definition schema and render deterministically - the same guarantees Golden
Replay will lean on."""

import json
from pathlib import Path

import nimbleship.engine.plugins  # noqa: F401  (fills the plugin registries)
from nimbleship.domain.carrier_definition import CarrierDefinition, PluginAuth
from nimbleship.engine.auth_plugins import AUTH_PLUGINS
from nimbleship.engine.render import render_operation

EXAMPLE = Path(__file__).parent.parent / "examples" / "fedex.definition.json"

FACTS: dict[str, object] = {
    "shipment": {
        "order_number": "95000254580",
        "ship_date": "2026-07-11",
        "proposition": "next-day",
        "recipient_name": "John Doe",
        "recipient_phone": "0033123456789",
        "address_lines": ["10 Rue de la Paix", "Quartier Vendome"],
        "city": "Paris",
        "postcode": "75002",
        "country": "FR",
        "parcels": [{"weight_kg": "4.20"}, {"weight_kg": "3.10"}],
    },
    "warehouse": {
        "name": "Acme Fulfilment Ltd",
        "contact_name": "Acme Fulfilment Ltd",
        "phone": "+44 116 000 0000",
        "address_lines": ["Unit 5", "Trading Estate"],
        "city": "Leicester",
        "postcode": "LE1 1AA",
        "country": "GB",
    },
    "config": {
        "ship_url": "https://apis.fedex.example/ship/v1/shipments",
        "token_url": "https://apis.fedex.example/oauth/token",
        "account_number": "802255209",
    },
}


def definition() -> CarrierDefinition:
    return CarrierDefinition.model_validate(json.loads(EXAMPLE.read_text()))


def test_the_example_validates_against_the_schema() -> None:
    fedex = definition()

    assert fedex.carrier == "fedex"


def test_auth_is_a_plugin_the_registry_actually_has() -> None:
    auth = definition().auth

    assert isinstance(auth, PluginAuth)
    assert auth.plugin in AUTH_PLUGINS


def test_booking_is_a_single_http_step_posting_json() -> None:
    [step] = definition().operations["book"].steps

    assert step.transport == "http"
    assert step.request.method == "POST"
    assert step.request.content_type == "json"


def test_the_render_carries_the_booking_shape() -> None:
    [request] = render_operation(definition(), "book", FACTS)

    assert request.url == "https://apis.fedex.example/ship/v1/shipments"
    assert request.body["labelResponseOptions"] == "LABEL"
    assert request.body["accountNumber"] == {"value": "802255209"}

    shipment = request.body["requestedShipment"]
    assert isinstance(shipment, dict)
    assert shipment["shipDatestamp"] == "2026-07-11"
    assert shipment["pickupType"] == "USE_SCHEDULED_PICKUP"
    assert shipment["packagingType"] == "YOUR_PACKAGING"
    assert shipment["shippingChargesPayment"] == {"paymentType": "SENDER"}
    assert shipment["labelSpecification"] == {
        "imageType": "PNG",
        "labelStockType": "PAPER_4X6",
    }


def test_the_service_type_is_looked_up_from_the_proposition() -> None:
    [request] = render_operation(definition(), "book", FACTS)

    shipment = request.body["requestedShipment"]
    assert isinstance(shipment, dict)
    assert shipment["serviceType"] == "INTERNATIONAL_PRIORITY"


def test_the_recipient_address_lines_are_joined_into_street_lines() -> None:
    [request] = render_operation(definition(), "book", FACTS)

    shipment = request.body["requestedShipment"]
    assert isinstance(shipment, dict)
    assert shipment["recipients"] == [
        {
            "contact": {
                "personName": "John Doe",
                "phoneNumber": "0033123456789",
            },
            "address": {
                "streetLines": ["10 Rue de la Paix, Quartier Vendome"],
                "city": "Paris",
                "postalCode": "75002",
                "countryCode": "FR",
                "residential": "true",
            },
        }
    ]


def test_the_shipper_block_comes_from_the_warehouse() -> None:
    [request] = render_operation(definition(), "book", FACTS)

    shipment = request.body["requestedShipment"]
    assert isinstance(shipment, dict)
    assert shipment["shipper"] == {
        "contact": {
            "personName": "Acme Fulfilment Ltd",
            "phoneNumber": "+44 116 000 0000",
            "companyName": "Acme Fulfilment Ltd",
        },
        "address": {
            "streetLines": ["Unit 5, Trading Estate"],
            "city": "Leicester",
            "postalCode": "LE1 1AA",
            "countryCode": "GB",
            "residential": "false",
        },
    }


def test_each_parcel_becomes_a_package_line_item_with_kg_weight() -> None:
    [request] = render_operation(definition(), "book", FACTS)

    shipment = request.body["requestedShipment"]
    assert isinstance(shipment, dict)
    assert shipment["requestedPackageLineItems"] == [
        {"weight": {"units": "KG", "value": "4.20"}},
        {"weight": {"units": "KG", "value": "3.10"}},
    ]


def test_labels_are_png_pages_extracted_from_the_response() -> None:
    fedex = definition()
    operation = fedex.operations["book"]
    [step] = operation.steps

    assert operation.label is not None
    assert operation.label.source == "png_pages"
    assert step.response is not None
    names = {extraction.name for extraction in step.response.extract}
    assert operation.label.from_extract in names
    assert "tracking_reference" in names


def test_the_render_is_deterministic() -> None:
    first = render_operation(definition(), "book", FACTS)
    second = render_operation(definition(), "book", FACTS)

    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]


def test_the_render_carries_no_credentials() -> None:
    """Plugin auth applies at execution time; a render (and so the Golden
    Replay corpus) must stay token- and secret-free."""
    [request] = render_operation(definition(), "book", FACTS)

    assert request.headers == {}
    assert "Authorization" not in request.headers
