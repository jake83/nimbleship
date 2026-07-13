"""Shared arrangement for the duplicate-order race tests: a real app over a
caller-supplied engine, dispatching through the Furdeco example carrier
behind an httpx.MockTransport whose handler commits a competing duplicate
consignment while the carrier call is in flight. The booking then succeeds
on the carrier's side but loses the unique-order flush - the scenario the
"carrier contact always commits traffic" invariant must survive. Used by
the file-backed SQLite test and its Postgres-gated twin."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from nimbleship.db import get_session
from nimbleship.http_client import get_http_client
from nimbleship.labels.store import LabelStore, get_label_store
from nimbleship.main import create_app
from nimbleship.models import Consignment

EXAMPLE = Path(__file__).parent.parent / "examples" / "furdeco.definition.json"

ORDER = "95000254580"

BOOKING_RESPONSE = (
    "<response>"
    "<success>Order Created</success>"
    "<carrier_reference>F12345678910</carrier_reference>"
    "<barcodes>001122334455667688, 123456789123456789</barcodes>"
    "</response>"
)

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

CONSIGNMENT_PAYLOAD = {
    "order_number": ORDER,
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],
}


def build_app(factory: "sessionmaker[Session]", labels_dir: Path) -> FastAPI:
    def session_override() -> Iterator[Session]:
        with factory() as session:
            yield session
            session.commit()

    app = create_app()
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_label_store] = lambda: LabelStore(labels_dir)
    return app


def publish_furdeco(client: TestClient) -> None:
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


def racing_carrier(
    app: FastAPI,
    factory: "sessionmaker[Session]",
    prepare_racer: Callable[[Session], None] | None = None,
) -> None:
    """Wire a mock carrier that answers success only after a competing
    session has committed a consignment for the same order number."""

    def winner_books_first(request: httpx.Request) -> httpx.Response:
        with factory() as racer:
            if prepare_racer is not None:
                prepare_racer(racer)
            racer.add(
                Consignment(
                    order_number=ORDER,
                    recipient_name="The Winner",
                    address_lines=["10 Downing Street", "London"],
                    postcode="SW1A 2AA",
                    destination_country="GB",
                    status="allocated",
                    carrier="furdeco",
                    service="FURDECO-2MAN",
                    allocation={},
                )
            )
            racer.commit()
        return httpx.Response(200, text=BOOKING_RESPONSE)

    def override() -> Iterator[httpx.Client]:
        with httpx.Client(
            transport=httpx.MockTransport(winner_books_first)
        ) as http_client:
            yield http_client

    app.dependency_overrides[get_http_client] = override
