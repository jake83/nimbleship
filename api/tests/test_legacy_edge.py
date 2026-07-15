"""The WMS-facing Legacy Interface (ADR 0002, ADR 0011): a SOAP/XML edge over
the same domain operations as the JSON API. The fixture here is a synthetic,
MetaPack-shaped request (real recorded traffic replaces it as operations land)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.models import LegacyConsignmentStaging

WMS_USER = "wms"
WMS_PASSWORD = "s3cret"

_FIXTURES = Path(__file__).parent / "fixtures" / "metapack"


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


@pytest.fixture
def wms_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    # get_settings reads the environment per request, so setting the credentials
    # after the app is built still gates the very next call.
    monkeypatch.setenv("NIMBLESHIP_LEGACY_WMS_USERNAME", WMS_USER)
    monkeypatch.setenv("NIMBLESHIP_LEGACY_WMS_PASSWORD", WMS_PASSWORD)
    yield (WMS_USER, WMS_PASSWORD)


def test_the_edge_rejects_a_request_with_no_credentials(client: TestClient) -> None:
    response = client.post(
        "/ConsignmentService",
        content=b"<soap:Envelope/>",
        headers={"Content-Type": "text/xml"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_the_edge_rejects_wrong_credentials(
    client: TestClient, wms_auth: tuple[str, str]
) -> None:
    response = client.post(
        "/ConsignmentService",
        content=b"<soap:Envelope/>",
        headers={"Content-Type": "text/xml"},
        auth=(WMS_USER, "wrong-password"),
    )

    assert response.status_code == 401


def test_the_edge_rejects_all_requests_when_unconfigured(client: TestClient) -> None:
    # No credential configured: the edge is closed, even with a Basic header, so
    # a fresh install never exposes the WMS surface by omission.
    response = client.post(
        "/ConsignmentService",
        content=b"<soap:Envelope/>",
        headers={"Content-Type": "text/xml"},
        auth=(WMS_USER, WMS_PASSWORD),
    )

    assert response.status_code == 401


def test_create_consignments_stages_the_shipment_and_returns_a_code(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    response = client.post(
        "/ConsignmentService",
        content=_fixture("create_consignments_request.xml"),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    # The WMS reads the assigned consignment code and Unallocated status back,
    # and reuses the code on the allocate and paperwork calls.
    assert "95000254580" in response.text
    assert "Unallocated" in response.text

    with app.state.session_factory() as session:
        rows = list(session.execute(select(LegacyConsignmentStaging)).scalars())
    assert len(rows) == 1
    staged = rows[0]
    assert staged.order_number == "95000254580"
    assert staged.consignment_code in response.text
    created = staged.created_data
    assert created is not None
    assert created["recipient_name"] == "Jane Doe"
    assert created["postcode"] == "SW1A 2AA"
    assert created["destination_country"] == "GB"
    assert created["warehouse"] == "DEPOT1"
    assert len(created["parcels"]) == 2


def test_create_consignments_faults_and_stages_nothing_without_an_order_number(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A consignment with no orderNumber is rejected with a fault, and the batch
    # rolls back so nothing is staged under a placeholder key.
    body = _fixture("create_consignments_request.xml").replace(
        b'<orderNumber xsi:type="xsd:string">95000254580</orderNumber>', b""
    )

    response = client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "Fault" in response.text
    assert "orderNumber" in response.text
    with app.state.session_factory() as session:
        assert not list(session.execute(select(LegacyConsignmentStaging)).scalars())


def test_the_edge_rejects_an_oversized_body(
    client: TestClient, wms_auth: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nimbleship.legacy.router._MAX_BODY_BYTES", 100)

    response = client.post(
        "/ConsignmentService",
        content=b"x" * 200,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 413
