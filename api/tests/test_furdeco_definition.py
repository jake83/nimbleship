"""The Furdeco example definition: the first live-API carrier expressed as
data (ADR 0009's proving ladder - single-call REST, query-key auth, XML
extraction). The example must validate against the schema and execute
end-to-end against the carrier's real response shape."""

import json
from pathlib import Path

import httpx

from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.engine.execute import execute_operation

EXAMPLE = Path(__file__).parent.parent / "examples" / "furdeco.definition.json"

# The carrier's booking response shape: a flat XML document whose
# carrier_reference is the tracking reference and whose barcodes arrive
# comma-joined in one element.
BOOKING_RESPONSE = """<?xml version="1.0"?>
<response>
    <success>Order Created</success>
    <location_code>S</location_code>
    <depot_code>F</depot_code>
    <carrier_reference>F12345678910</carrier_reference>
    <postcode>RG40 2LF</postcode>
    <barcodes>001122334455667688, 123456789123456789, 987654321987654321</barcodes>
    <labels>https://labels.furdeco.example/?carrier_reference=F12345678910</labels>
    <tracking_link>https://track.furdeco.example/?ordernumber=F12345678910</tracking_link>
</response>
"""

FACTS: dict[str, object] = {
    "shipment": {
        "order_number": "95000254580",
        "recipient_name": "John Doe",
        "address_lines": ["10 Downing Street", "London"],
        "postcode": "rg40 2lf",
        "destination_country": "GB",
        "parcels": [{"weight_kg": "4.2", "barcode": "95000254580-1"}],
    },
    "config": {
        "api_key": "SECRET-KEY",
        "base_url": "https://api.furdeco.example/orders",
        "trading_name": "Acme Trading",
    },
}


def _definition() -> CarrierDefinition:
    return CarrierDefinition.model_validate(json.loads(EXAMPLE.read_text()))


def test_the_example_validates_against_the_schema() -> None:
    definition = _definition()

    assert definition.carrier == "furdeco"
    assert definition.operations["book"].label is not None
    assert definition.operations["book"].label.source == "local_render"


def test_the_example_books_end_to_end_against_the_carrier_response_shape() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=BOOKING_RESPONSE)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = execute_operation(_definition(), "book", FACTS, client)

    [request] = seen
    assert request.method == "POST"
    assert request.url.copy_with(params=None) == "https://api.furdeco.example/orders"
    assert request.url.params["action"] == "save"
    assert request.url.params["key"] == "SECRET-KEY"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    fields = dict(pair.split("=", 1) for pair in request.content.decode().split("&"))
    assert fields["OrderNumber"] == "95000254580"
    assert fields["TradingName"] == "Acme+Trading"
    assert fields["Recipient"] == "John+Doe"
    assert fields["Address"] == "10+Downing+Street%2C+London"
    assert fields["Postcode"] == "RG40+2LF"
    assert fields["ServiceLevel"] == "2+Man"

    assert result.outputs["tracking_reference"] == "F12345678910"
    assert result.outputs["barcodes"] == [
        "001122334455667688",
        "123456789123456789",
        "987654321987654321",
    ]
