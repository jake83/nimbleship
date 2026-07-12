"""Dispatch through a live-API carrier: the book operation's http steps
execute at consignment creation, the tracking reference and carrier
barcodes land on the consignment, every request/response is recorded as
carrier traffic, and a failed carrier call is loud - 502, never a silent
success. The carrier here is the Furdeco example definition over an
httpx.MockTransport: zero real network."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.http_client import get_http_client
from nimbleship.models import CarrierTraffic, Consignment

EXAMPLE = Path(__file__).parent.parent / "examples" / "furdeco.definition.json"

BOOKING_RESPONSE = (
    "<response>"
    "<success>Order Created</success>"
    "<carrier_reference>F12345678910</carrier_reference>"
    "<barcodes>001122334455667688, 123456789123456789</barcodes>"
    "</response>"
)

ERROR_RESPONSE = "<response><error>Postcode not covered</error></response>"

RULEBOOK_DRAFT = {
    "author": "jake",
    "services": [
        {
            "code": "FURDECO-2MAN",
            "carrier": "furdeco",
            "name": "Furdeco Two Man",
            "weight_min_kg": "0",
            "weight_max_kg": "999",
            "countries": ["GB"],
            "cost": "25.00",
            "tie_break_order": 1,
        }
    ],
}

CONSIGNMENT = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],
}


def _publish_furdeco(client: TestClient) -> None:
    definition = json.loads(EXAMPLE.read_text())
    response = client.put(
        "/api/carriers/furdeco/config",
        json={
            "api_key": "SECRET-KEY",
            "base_url": "https://api.furdeco.example/orders",
            "trading_name": "Acme Trading",
        },
    )
    assert response.status_code == 200
    response = client.post(
        "/api/carriers/furdeco/definitions/drafts",
        json={"author": "jake", "definition": definition},
    )
    assert response.status_code == 201
    version = response.json()["version"]
    response = client.post(
        f"/api/carriers/furdeco/definitions/versions/{version}/publish"
    )
    assert response.status_code == 200
    version = client.post("/api/rulebook/drafts", json=RULEBOOK_DRAFT).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def _carrier_answers(
    app: FastAPI, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def override() -> Iterator[httpx.Client]:
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            yield http_client

    app.dependency_overrides[get_http_client] = override


@pytest.fixture
def furdeco_client(app: FastAPI, client: TestClient) -> TestClient:
    _publish_furdeco(client)
    _carrier_answers(app, lambda request: httpx.Response(200, text=BOOKING_RESPONSE))
    return client


def test_booking_stores_tracking_reference_and_carrier_barcodes(
    furdeco_client: TestClient,
) -> None:
    response = furdeco_client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "allocated"
    assert body["carrier"] == "furdeco"
    assert body["tracking_reference"] == "F12345678910"

    detail = furdeco_client.get("/api/consignments/95000254580").json()
    assert detail["tracking_reference"] == "F12345678910"
    assert [p["carrier_barcode"] for p in detail["parcels"]] == [
        "001122334455667688",
        "123456789123456789",
    ]


def test_booking_adds_a_booked_event_with_the_step_outcomes(
    furdeco_client: TestClient,
) -> None:
    furdeco_client.post("/api/consignments", json=CONSIGNMENT)

    detail = furdeco_client.get("/api/consignments/95000254580").json()
    stages = [e["stage"] for e in detail["events"]]
    assert stages == ["allocated", "booked", "label_created"]
    [booked] = [e for e in detail["events"] if e["stage"] == "booked"]
    assert booked["detail"]["carrier"] == "furdeco"
    assert booked["detail"]["tracking_reference"] == "F12345678910"
    assert booked["detail"]["steps"] == [
        {"step": "save", "status": 200, "success": True}
    ]


def test_booking_still_renders_the_local_label(furdeco_client: TestClient) -> None:
    furdeco_client.post("/api/consignments", json=CONSIGNMENT)

    label = furdeco_client.get("/api/consignments/95000254580/label.pdf")

    assert label.status_code == 200
    assert label.content.startswith(b"%PDF")


def test_booking_records_the_traffic(app: FastAPI, furdeco_client: TestClient) -> None:
    furdeco_client.post("/api/consignments", json=CONSIGNMENT)

    with app.state.session_factory() as session:
        [row] = session.execute(select(CarrierTraffic)).scalars().all()
        assert row.carrier == "furdeco"
        assert row.order_number == "95000254580"
        assert row.step == "save"
        assert row.request["body"]["OrderNumber"] == "95000254580"
        assert row.request["query"] == {"action": "save", "key": "SECRET-KEY"}
        assert row.response_status == 200
        assert "F12345678910" in row.response_body


def test_a_failed_carrier_call_is_a_502_with_the_carrier_message(
    app: FastAPI, client: TestClient
) -> None:
    _publish_furdeco(client)
    _carrier_answers(app, lambda request: httpx.Response(200, text=ERROR_RESPONSE))

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 502
    assert response.json()["detail"] == "Postcode not covered"

    # Never a silent success: the consignment is kept, marked failed, with
    # the error on the timeline and the traffic recorded.
    detail = client.get("/api/consignments/95000254580").json()
    assert detail["status"] == "booking_failed"
    assert detail["label_url"] is None
    stages = [e["stage"] for e in detail["events"]]
    assert stages == ["allocated", "booking_failed"]
    [failed] = [e for e in detail["events"] if e["stage"] == "booking_failed"]
    assert failed["detail"]["error"] == "Postcode not covered"

    with app.state.session_factory() as session:
        [row] = session.execute(select(CarrierTraffic)).scalars().all()
        assert row.response_status == 200
        assert "Postcode not covered" in row.response_body
        consignment = session.execute(select(Consignment)).scalar_one()
        assert consignment.status == "booking_failed"


def test_extra_carrier_barcodes_are_kept_in_the_booked_event(
    app: FastAPI, client: TestClient
) -> None:
    three_barcodes = (
        "<response>"
        "<carrier_reference>F12345678910</carrier_reference>"
        "<barcodes>B-1, B-2, B-3</barcodes>"
        "</response>"
    )
    _publish_furdeco(client)
    _carrier_answers(app, lambda request: httpx.Response(200, text=three_barcodes))

    client.post("/api/consignments", json=CONSIGNMENT)

    detail = client.get("/api/consignments/95000254580").json()
    # Two parcels get the first two barcodes positionally; nothing is lost -
    # the full list lives on the booked event.
    assert [p["carrier_barcode"] for p in detail["parcels"]] == ["B-1", "B-2"]
    [booked] = [e for e in detail["events"] if e["stage"] == "booked"]
    assert booked["detail"]["barcodes"] == ["B-1", "B-2", "B-3"]


def test_a_stepless_carrier_records_no_traffic(
    app: FastAPI, client: TestClient
) -> None:
    # dropout books with local_render only: no http steps, no carrier call.
    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    assert response.json()["carrier"] == "dropout"
    assert response.json()["tracking_reference"] is None
    with app.state.session_factory() as session:
        assert session.execute(select(CarrierTraffic)).scalars().all() == []
