"""Booking a consignment with an FTP carrier (Fagans): the book operation's
ftp_upload step renders a CSV and hands it to the file uploader, the local
label still renders, the upload is recorded as carrier traffic, and - since
Fagans is fire-and-forget - no tracking reference comes back. The uploader
is a fake: the suite never opens an FTP connection."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.models import CarrierTraffic, Consignment
from nimbleship.uploaders import UploadError, get_carrier_uploaders

EXAMPLE = Path(__file__).parent.parent / "examples" / "fagans.definition.json"

CONFIG = {
    "account_code": "LIM2",
    "ftp_remote_dir": "/outbound",
    "ftp_host": "ftp.fagans.example",
    "ftp_username": "nimbleship",
    "ftp_password": "SECRET-PW",
}

RULEBOOK_DRAFT = {
    "author": "jake",
    "services": [
        {
            "code": "FAGANS-PALLET",
            "carrier": "fagans",
            "name": "Fagans Pallet",
            "weight_min_kg": "0",
            "weight_max_kg": "999",
            "countries": ["GB"],
            "cost": "35.00",
            "tie_break_order": 1,
        }
    ],
}

CONSIGNMENT = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "sw1a 2aa",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "120.0"}],
}


class _FakeUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, object], str, str, str]] = []
        self.fail_with: str | None = None

    def upload(
        self,
        config: dict[str, object],
        remote_path: str,
        filename: str,
        content: str,
    ) -> None:
        self.calls.append((config, remote_path, filename, content))
        if self.fail_with is not None:
            raise UploadError(self.fail_with)


def _publish_fagans(client: TestClient) -> None:
    definition = json.loads(EXAMPLE.read_text())
    assert client.put("/api/carriers/fagans/config", json=CONFIG).status_code == 200
    response = client.post(
        "/api/carriers/fagans/definitions/drafts",
        json={"author": "jake", "definition": definition},
    )
    assert response.status_code == 201
    version = response.json()["version"]
    assert (
        client.post(
            f"/api/carriers/fagans/definitions/versions/{version}/publish"
        ).status_code
        == 200
    )
    version = client.post("/api/rulebook/drafts", json=RULEBOOK_DRAFT).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


@pytest.fixture
def uploader(app: FastAPI) -> _FakeUploader:
    fake = _FakeUploader()
    app.dependency_overrides[get_carrier_uploaders] = lambda: {"ftp_upload": fake}
    return fake


@pytest.fixture
def fagans_client(client: TestClient, uploader: _FakeUploader) -> TestClient:
    _publish_fagans(client)
    return client


def test_booking_uploads_the_rendered_csv_to_the_carrier(
    fagans_client: TestClient, uploader: _FakeUploader
) -> None:
    response = fagans_client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    body = response.json()
    # fagans declares no manifest operation, so it dispatches at label time.
    assert body["status"] == "dispatched"
    assert body["carrier"] == "fagans"
    # Fire-and-forget: Fagans returns nothing to track by.
    assert body["tracking_reference"] is None

    [(config, remote_path, filename, content)] = uploader.calls
    assert remote_path == "/outbound"
    assert filename == "DMC95000254580.csv"
    assert content == (
        "LIM2,DMC95000254580,95000254580,John Doe,"
        '"10 Downing Street, London",SW1A 2AA,GB\r\n'
    )
    # Credentials reach the uploader via config, to connect with.
    assert config["ftp_host"] == "ftp.fagans.example"


def test_booking_still_renders_the_local_label(fagans_client: TestClient) -> None:
    fagans_client.post("/api/consignments", json=CONSIGNMENT)

    label = fagans_client.get("/api/consignments/95000254580/label.pdf")

    assert label.status_code == 200
    assert label.content.startswith(b"%PDF")


def test_booking_records_the_upload_as_traffic_without_credentials(
    app: FastAPI, fagans_client: TestClient
) -> None:
    fagans_client.post("/api/consignments", json=CONSIGNMENT)

    with app.state.session_factory() as session:
        [row] = session.execute(select(CarrierTraffic)).scalars().all()
        assert row.carrier == "fagans"
        assert row.order_number == "95000254580"
        assert row.step == "upload"
        # Fire-and-forget upload: no HTTP status.
        assert row.response_status is None
        assert row.request["content_type"] == "csv"
        assert row.request["filename"] == "DMC95000254580.csv"
        # The rendered file is the golden corpus; the password is not in it.
        assert "SECRET-PW" not in json.dumps(row.request)


def test_a_failed_upload_is_a_502_and_marks_the_consignment_failed(
    app: FastAPI, client: TestClient, uploader: _FakeUploader
) -> None:
    _publish_fagans(client)
    uploader.fail_with = "530 Login incorrect"

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 502
    assert "530 Login incorrect" in response.json()["detail"]
    detail = client.get("/api/consignments/95000254580").json()
    assert detail["status"] == "booking_failed"
    assert detail["label_url"] is None
    with app.state.session_factory() as session:
        consignment = session.execute(select(Consignment)).scalar_one()
        assert consignment.status == "booking_failed"
        [row] = session.execute(select(CarrierTraffic)).scalars().all()
        assert "530 Login incorrect" in row.response_body
