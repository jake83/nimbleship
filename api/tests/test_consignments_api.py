import io

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

CONSIGNMENT = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],
}


def test_creating_a_consignment_allocates_and_produces_a_label(
    client: TestClient,
) -> None:
    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "allocated"
    assert body["carrier"] == "dropout"
    assert body["service"] == "DROPOUT-STD"
    assert body["label_url"] == "/api/consignments/95000254580/label.pdf"
    assert body["allocation"]["reason"] == "cheapest eligible service"
    assert body["allocation"]["rulebook_version"] == 1


def test_label_pdf_is_downloadable(client: TestClient) -> None:
    client.post("/api/consignments", json=CONSIGNMENT)

    response = client.get("/api/consignments/95000254580/label.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_timeline_records_allocation_and_label_creation(
    client: TestClient,
) -> None:
    client.post("/api/consignments", json=CONSIGNMENT)

    response = client.get("/api/consignments/95000254580")

    assert response.status_code == 200
    body = response.json()
    assert [e["stage"] for e in body["events"]] == ["allocated", "label_created"]
    assert body["parcels"] == [
        {"sequence": 1, "weight_kg": "4.2", "barcode": "95000254580-1"},
        {"sequence": 2, "weight_kg": "3.1", "barcode": "95000254580-2"},
    ]


def test_heavy_consignment_selects_the_larger_service(client: TestClient) -> None:
    heavy = {
        **CONSIGNMENT,
        "order_number": "95000254581",
        "parcels": [{"weight_kg": "40"}],
    }

    response = client.post("/api/consignments", json=heavy)

    assert response.status_code == 201
    assert response.json()["service"] == "DROPOUT-XL"


def test_unservable_destination_is_recorded_as_rejected(
    client: TestClient,
) -> None:
    rejected = {
        **CONSIGNMENT,
        "order_number": "95000254582",
        "destination_country": "US",
    }

    response = client.post("/api/consignments", json=rejected)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "rejected"
    assert body["service"] is None
    assert body["allocation"]["reason"] == "no eligible services"

    timeline = client.get("/api/consignments/95000254582").json()
    assert [e["stage"] for e in timeline["events"]] == ["rejected"]


def test_duplicate_order_number_conflicts(client: TestClient) -> None:
    client.post("/api/consignments", json=CONSIGNMENT)

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 409


def test_unknown_consignment_is_not_found(client: TestClient) -> None:
    assert client.get("/api/consignments/nope").status_code == 404
    assert client.get("/api/consignments/nope/label.pdf").status_code == 404


def test_non_latin_order_numbers_are_rejected_with_422(client: TestClient) -> None:
    unicode_order = {**CONSIGNMENT, "order_number": "订单1"}

    response = client.post("/api/consignments", json=unicode_order)

    assert response.status_code == 422


WAREHOUSE = {
    "code": "MAIN",
    "name": "Main Warehouse",
    "company_name": "Acme Fulfilment Ltd",
    "address_lines": ["Unit 5, Trading Estate"],
    "postcode": "LE1 1AA",
    "country": "GB",
}


def _label_text(client: TestClient, order_number: str) -> str:
    pdf = client.get(f"/api/consignments/{order_number}/label.pdf").content
    return PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


def test_consignment_stores_its_warehouse_and_labels_carry_its_sender_details(
    client: TestClient,
) -> None:
    client.post("/api/warehouses", json=WAREHOUSE)

    response = client.post(
        "/api/consignments", json={**CONSIGNMENT, "warehouse": "MAIN"}
    )

    assert response.status_code == 201
    assert response.json()["warehouse"] == "MAIN"
    assert client.get("/api/consignments/95000254580").json()["warehouse"] == "MAIN"
    text = _label_text(client, "95000254580")
    assert "Acme Fulfilment Ltd" in text
    assert "LE1 1AA" in text


def test_consignment_without_a_warehouse_has_no_sender_details(
    client: TestClient,
) -> None:
    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    assert response.json()["warehouse"] is None
    assert "From" not in _label_text(client, "95000254580")


def test_unknown_warehouse_code_is_rejected_and_stores_nothing(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/consignments", json={**CONSIGNMENT, "warehouse": "NOPE"}
    )

    assert response.status_code == 422
    assert client.get("/api/consignments/95000254580").status_code == 404


def test_losing_a_duplicate_race_still_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nimbleship.routers.consignments as consignments_module

    client.post("/api/consignments", json=CONSIGNMENT)
    # Simulate the race: the existence pre-check misses the row that another
    # request has already committed, so the unique constraint is the last line
    # of defence.
    monkeypatch.setattr(
        consignments_module, "_order_exists", lambda session, order_number: False
    )

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 409
