"""Dispatch confirmation to Manifest (CONTEXT.md: Manifest): the WMS
confirms consignments, NimbleShip groups the manifest-carrier ones per
carrier and warehouse into Manifests (moving them to "on_manifest", not yet
dispatched - the send does that, ADR 0013), and enqueues one send job per
Manifest in the same transaction (ADR 0004). A non-manifest consignment is
already dispatched from paperwork, so naming it is a no-op. The queue here is
Procrastinate's in-memory test connector: jobs are recorded, never executed."""

from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from procrastinate.testing import InMemoryConnector
from sqlalchemy import select

from nimbleship.models import Consignment, Manifest

MANIFESTING_DEFINITION = {
    "carrier": "brightpost",
    "name": "Bright Post",
    "auth": {"scheme": "header_key", "header": "X-Api-Key", "secret": "config.api_key"},
    "operations": {
        "book": {
            "steps": [],
            "label": {"source": "local_render", "template": "standard_a6"},
        },
        "manifest": {
            "steps": [
                {
                    "name": "declare",
                    "transport": "http",
                    "request": {
                        "method": "POST",
                        "url": "config.manifest_url",
                        "content_type": "json",
                        "mapping": [
                            {"target": "date", "source": "manifest.date"},
                            {"target": "count", "source": "manifest.consignment_count"},
                            {
                                "target": "orders",
                                "source": "manifest.consignments",
                                "each": [
                                    {"target": "order", "source": "item.order_number"},
                                    {
                                        "target": "parcels",
                                        "source": "item.parcel_count",
                                    },
                                ],
                            },
                        ],
                    },
                    "response": {
                        "format": "json",
                        "success_when": {"path": "manifest_id"},
                        "error_message": {"path": "error"},
                        "extract": [
                            {"name": "manifest_reference", "path": "manifest_id"}
                        ],
                    },
                }
            ]
        },
    },
}

# brightpost takes light consignments, dropout heavy ones: one rulebook,
# two carriers, so a single confirmation can span both.
RULEBOOK_DRAFT = {
    "author": "jake",
    "services": [
        {
            "code": "BP-STD",
            "carrier": "brightpost",
            "name": "Bright Post Standard",
            "weight_min_kg": "0",
            "weight_max_kg": "30",
            "countries": ["GB"],
            "cost": "4.50",
            "tie_break_order": 1,
        },
        {
            "code": "DO-HEAVY",
            "carrier": "dropout",
            "name": "Drop Out Heavy",
            "weight_min_kg": "30",
            "weight_max_kg": "999",
            "countries": ["GB"],
            "cost": "25.00",
            "tie_break_order": 2,
        },
    ],
}


def _consignment(order_number: str, weight: str = "4.2") -> dict[str, object]:
    return {
        "order_number": order_number,
        "recipient_name": "John Doe",
        "address_lines": ["10 Downing Street", "London"],
        "postcode": "SW1A 2AA",
        "destination_country": "GB",
        "parcels": [{"weight_kg": weight}],
    }


def _publish_brightpost(client: TestClient) -> None:
    response = client.put(
        "/api/carriers/brightpost/config",
        json={
            "api_key": "SECRET-KEY",
            "manifest_url": "https://api.brightpost.example/manifests",
        },
    )
    assert response.status_code == 200
    response = client.post(
        "/api/carriers/brightpost/definitions/drafts",
        json={"author": "jake", "definition": MANIFESTING_DEFINITION},
    )
    assert response.status_code == 201
    version = response.json()["version"]
    response = client.post(
        f"/api/carriers/brightpost/definitions/versions/{version}/publish"
    )
    assert response.status_code == 200
    version = client.post("/api/rulebook/drafts", json=RULEBOOK_DRAFT).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


@pytest.fixture
def brightpost_client(client: TestClient) -> TestClient:
    _publish_brightpost(client)
    return client


def test_a_manifest_definition_publishes_with_order_history_present(
    client: TestClient,
) -> None:
    # The publish render gate renders every shipment-context operation
    # against recent consignments. A manifest operation renders from
    # manifest facts, not shipment facts, so it must be skipped by the gate
    # - otherwise re-publishing a manifest-capable definition once orders
    # exist would be refused (the gate would find no shipment.* facts for
    # manifest.date). Publish with history present to pin that.
    _publish_brightpost(client)
    assert (
        client.post("/api/consignments", json=_consignment("HIST-1")).status_code == 201
    )

    definition = client.post(
        "/api/carriers/brightpost/definitions/drafts",
        json={"author": "jake", "definition": MANIFESTING_DEFINITION},
    )
    assert definition.status_code == 201
    version = definition.json()["version"]

    published = client.post(
        f"/api/carriers/brightpost/definitions/versions/{version}/publish"
    )
    assert published.status_code == 200


def _queue_jobs(app: FastAPI) -> list[dict[str, object]]:
    connector = cast(InMemoryConnector, app.state.queue_connector)
    return list(connector.jobs.values())


def test_dispatch_confirmation_manifests_per_carrier(
    app: FastAPI, brightpost_client: TestClient
) -> None:
    for order, weight in (("A-1", "4.2"), ("A-2", "8.0"), ("A-3", "45.0")):
        assert (
            brightpost_client.post(
                "/api/consignments", json=_consignment(order, weight)
            ).status_code
            == 201
        )

    # dropout (the heavy A-3) has no manifest operation, so it is already
    # dispatched from its create call, before any confirmation (ADR 0013).
    a3_before = brightpost_client.get("/api/consignments/A-3").json()
    assert a3_before["status"] == "dispatched"
    assert a3_before["events"][-1]["stage"] == "dispatched"

    response = brightpost_client.post(
        "/api/dispatch-confirmations",
        json={"order_numbers": ["A-1", "A-2", "A-3"]},
    )

    assert response.status_code == 201
    body = response.json()
    assert sorted(body["confirmed"]) == ["A-1", "A-2", "A-3"]
    # brightpost's two share one Manifest; A-3 (dropout) manifests nothing and
    # rides through as an already-dispatched no-op.
    [manifest] = body["manifests"]
    assert manifest["carrier"] == "brightpost"
    assert manifest["status"] == "pending"
    assert manifest["order_numbers"] == ["A-1", "A-2"]

    # A manifest carrier's consignments are on_manifest, not yet dispatched -
    # the send does that; A-3 is untouched by the confirmation.
    for order in ("A-1", "A-2"):
        detail = brightpost_client.get(f"/api/consignments/{order}").json()
        assert detail["status"] == "on_manifest"
        assert detail["events"][-1]["stage"] == "on_manifest"
    a3_after = brightpost_client.get("/api/consignments/A-3").json()
    assert a3_after["status"] == "dispatched"
    assert a3_after["events"][-1]["stage"] == "dispatched"


def test_dispatch_confirmation_enqueues_one_send_job_per_manifest(
    app: FastAPI, brightpost_client: TestClient
) -> None:
    brightpost_client.post("/api/consignments", json=_consignment("B-1"))

    response = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["B-1"]}
    )

    assert response.status_code == 201
    [manifest] = response.json()["manifests"]
    [job] = _queue_jobs(app)
    assert job["task_name"] == "manifests.send"
    assert job["args"] == {"manifest_id": manifest["id"]}


def test_manifests_split_per_warehouse(
    app: FastAPI, brightpost_client: TestClient
) -> None:
    for code in ("WH-A", "WH-B"):
        assert (
            brightpost_client.post(
                "/api/warehouses",
                json={
                    "code": code,
                    "name": f"Warehouse {code}",
                    "address_lines": ["1 Dock Road"],
                    "postcode": "M1 1AA",
                    "country": "GB",
                },
            ).status_code
            == 201
        )
    for order, warehouse in (("W-1", "WH-A"), ("W-2", "WH-B")):
        payload = _consignment(order) | {"warehouse": warehouse}
        assert (
            brightpost_client.post("/api/consignments", json=payload).status_code == 201
        )

    response = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["W-1", "W-2"]}
    )

    assert response.status_code == 201
    manifests = response.json()["manifests"]
    assert [(m["warehouse"], m["order_numbers"]) for m in manifests] == [
        ("WH-A", ["W-1"]),
        ("WH-B", ["W-2"]),
    ]
    assert len(_queue_jobs(app)) == 2


def test_unknown_order_numbers_reject_the_whole_confirmation(
    app: FastAPI, brightpost_client: TestClient
) -> None:
    brightpost_client.post("/api/consignments", json=_consignment("C-1"))

    response = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["C-1", "GHOST-1"]}
    )

    assert response.status_code == 422
    assert "GHOST-1" in response.json()["detail"]
    # Nothing dispatched, nothing enqueued: the confirmation is transactional.
    detail = brightpost_client.get("/api/consignments/C-1").json()
    assert detail["status"] == "allocated"
    assert _queue_jobs(app) == []
    with app.state.session_factory() as session:
        assert session.execute(select(Manifest)).scalars().all() == []


def test_undispatchable_consignments_reject_the_whole_confirmation(
    app: FastAPI, brightpost_client: TestClient
) -> None:
    brightpost_client.post("/api/consignments", json=_consignment("D-1"))
    # No GB coverage above 999kg: rejected, so it never physically ships.
    brightpost_client.post("/api/consignments", json=_consignment("D-2", "1500"))

    response = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["D-1", "D-2"]}
    )

    assert response.status_code == 409
    assert "D-2" in response.json()["detail"]
    detail = brightpost_client.get("/api/consignments/D-1").json()
    assert detail["status"] == "allocated"
    assert _queue_jobs(app) == []


def test_a_consignment_cannot_be_dispatched_twice(
    app: FastAPI, brightpost_client: TestClient
) -> None:
    brightpost_client.post("/api/consignments", json=_consignment("E-1"))
    first = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["E-1"]}
    )
    assert first.status_code == 201

    response = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["E-1"]}
    )

    assert response.status_code == 409
    assert "E-1" in response.json()["detail"]
    with app.state.session_factory() as session:
        manifests = session.execute(select(Manifest)).scalars().all()
        assert len(manifests) == 1


def test_duplicate_order_numbers_in_one_confirmation_are_rejected(
    brightpost_client: TestClient,
) -> None:
    brightpost_client.post("/api/consignments", json=_consignment("F-1"))

    response = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["F-1", "F-1"]}
    )

    assert response.status_code == 422
    assert "F-1" in response.json()["detail"]


def test_confirming_an_already_dispatched_consignment_is_a_no_op(
    app: FastAPI, brightpost_client: TestClient
) -> None:
    # dropout (heavy G-1) has no manifest operation, so it dispatched at create;
    # a confirmation naming it is an idempotent no-op - no Manifest, no job, and
    # it stays dispatched (ADR 0013).
    brightpost_client.post("/api/consignments", json=_consignment("G-1", "45.0"))
    before = brightpost_client.get("/api/consignments/G-1").json()
    assert before["status"] == "dispatched"

    response = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["G-1"]}
    )

    assert response.status_code == 201
    assert response.json()["confirmed"] == ["G-1"]
    assert response.json()["manifests"] == []
    assert _queue_jobs(app) == []
    with app.state.session_factory() as session:
        consignment = session.execute(select(Consignment)).scalar_one()
        assert consignment.status == "dispatched"


def test_manifests_are_listable_and_fetchable(
    brightpost_client: TestClient,
) -> None:
    brightpost_client.post("/api/consignments", json=_consignment("H-1"))
    created = brightpost_client.post(
        "/api/dispatch-confirmations", json={"order_numbers": ["H-1"]}
    )
    [manifest] = created.json()["manifests"]

    listing = brightpost_client.get("/api/manifests")
    assert listing.status_code == 200
    assert [m["id"] for m in listing.json()] == [manifest["id"]]

    detail = brightpost_client.get(f"/api/manifests/{manifest['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["carrier"] == "brightpost"
    assert body["status"] == "pending"
    assert body["order_numbers"] == ["H-1"]
    assert body["attempts"] == 0
    assert body["last_error"] is None
    assert body["sent_at"] is None

    assert brightpost_client.get("/api/manifests/9999").status_code == 404
