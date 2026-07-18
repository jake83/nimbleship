"""Tracking webhooks (ADR 0014): a source posts tracking updates, its adapter
normalises them onto the canonical status vocabulary, and they land in the
dedicated Tracking Event store - idempotent on the source's own event id."""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.models import TrackingEvent

SECRET = "voila-secret"


@pytest.fixture
def voila_secret(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    # get_settings reads the environment per request, so setting the secret
    # after the app is built still gates the very next call.
    monkeypatch.setenv("NIMBLESHIP_VOILA_WEBHOOK_SECRET", SECRET)
    yield SECRET


def _voila_payload(
    order_number: str = "95000254580",
    events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if events is None:
        events = [
            {
                "status_code": 4,
                "update_id": "EV-1",
                "update_date": "2026-07-18T09:00:00",
            },
            {
                "status_code": 7,
                "update_id": "EV-2",
                "update_date": "2026-07-18T14:30:00",
            },
        ]
    return {
        "tracking_update": {
            "shipment_id": "VOILA-SHIP-1",
            "shipment": {"reference": order_number},
            "parcels": [{"tracking_code": "TRK-1", "tracking_events": events}],
        }
    }


def _stored(app: FastAPI) -> list[TrackingEvent]:
    with app.state.session_factory() as session:
        return list(
            session.execute(select(TrackingEvent).order_by(TrackingEvent.id)).scalars()
        )


def test_voila_webhook_ingests_and_normalises_events(
    app: FastAPI, client: TestClient, voila_secret: str
) -> None:
    response = client.post(
        "/api/tracking/webhooks/voila",
        json=_voila_payload(),
        headers={"X-Webhook-Secret": voila_secret},
    )

    assert response.status_code == 200
    assert response.json() == {"events_stored": 2}
    events = _stored(app)
    assert [(e.raw_status, e.status) for e in events] == [
        ("4", "in_transit"),
        ("7", "delivered"),
    ]
    first = events[0]
    assert first.order_number == "95000254580"
    assert first.source == "voila"
    assert first.external_id == "EV-1"
    assert first.source_shipment_id == "VOILA-SHIP-1"
    assert first.tracking_code == "TRK-1"
    assert first.event_at is not None
    assert first.raw["status_code"] == 4


def test_an_unmapped_status_code_normalises_to_unknown(
    app: FastAPI, client: TestClient, voila_secret: str
) -> None:
    payload = _voila_payload(
        events=[{"status_code": 999, "update_id": "EV-X", "update_date": None}]
    )

    response = client.post(
        "/api/tracking/webhooks/voila",
        json=payload,
        headers={"X-Webhook-Secret": voila_secret},
    )

    assert response.status_code == 200
    [event] = _stored(app)
    assert event.raw_status == "999"
    assert event.status == "unknown"
    assert event.event_at is None


def test_redelivered_events_are_not_stored_twice(
    app: FastAPI, client: TestClient, voila_secret: str
) -> None:
    # Webhooks redeliver; ingestion is idempotent on (source, external_id).
    for _ in range(2):
        response = client.post(
            "/api/tracking/webhooks/voila",
            json=_voila_payload(),
            headers={"X-Webhook-Secret": voila_secret},
        )
        assert response.status_code == 200
    assert response.json() == {"events_stored": 0}
    assert len(_stored(app)) == 2


def test_partial_events_missing_a_status_or_id_are_skipped(
    app: FastAPI, client: TestClient, voila_secret: str
) -> None:
    payload = _voila_payload(
        events=[
            {"status_code": 7, "update_id": "EV-1", "update_date": None},
            {"update_id": "EV-2", "update_date": None},  # no status_code
            {"status_code": 5, "update_date": None},  # no update_id
        ]
    )

    response = client.post(
        "/api/tracking/webhooks/voila",
        json=payload,
        headers={"X-Webhook-Secret": voila_secret},
    )

    assert response.status_code == 200
    assert response.json() == {"events_stored": 1}


def test_the_webhook_faults_on_a_payload_with_no_order_reference(
    client: TestClient, voila_secret: str
) -> None:
    response = client.post(
        "/api/tracking/webhooks/voila",
        json={"tracking_update": {"shipment_id": "S1", "parcels": []}},
        headers={"X-Webhook-Secret": voila_secret},
    )

    assert response.status_code == 422
    assert "reference" in response.text


def test_the_webhook_rejects_a_missing_or_wrong_secret(
    client: TestClient, voila_secret: str
) -> None:
    assert (
        client.post("/api/tracking/webhooks/voila", json=_voila_payload()).status_code
        == 401
    )
    assert (
        client.post(
            "/api/tracking/webhooks/voila",
            json=_voila_payload(),
            headers={"X-Webhook-Secret": "wrong"},
        ).status_code
        == 401
    )


def test_the_webhook_is_closed_until_the_secret_is_configured(
    client: TestClient,
) -> None:
    # No secret set: the webhook rejects even a caller that sends one.
    response = client.post(
        "/api/tracking/webhooks/voila",
        json=_voila_payload(),
        headers={"X-Webhook-Secret": SECRET},
    )

    assert response.status_code == 401


def test_an_unknown_source_is_rejected_like_a_bad_secret(
    client: TestClient, voila_secret: str
) -> None:
    # An unknown source has no configured secret, so it 401s rather than 404ing -
    # an unauthenticated caller cannot enumerate which sources exist.
    response = client.post(
        "/api/tracking/webhooks/nope",
        json=_voila_payload(),
        headers={"X-Webhook-Secret": voila_secret},
    )

    assert response.status_code == 401


def test_an_over_length_field_is_rejected_before_it_reaches_the_db(
    app: FastAPI, client: TestClient, voila_secret: str
) -> None:
    # A source field longer than its column is a clean 422, not a driver error
    # the savepoint would miss on Postgres; the whole delivery is rejected and
    # nothing is stored (the guard runs before any insert).
    payload = _voila_payload(order_number="X" * 200)

    response = client.post(
        "/api/tracking/webhooks/voila",
        json=payload,
        headers={"X-Webhook-Secret": voila_secret},
    )

    assert response.status_code == 422
    assert "order number" in response.text
    assert _stored(app) == []


def test_voila_events_carry_a_utc_aware_timestamp() -> None:
    # A source timestamp with no offset is pinned to UTC, so the tz-aware column
    # stores an unambiguous instant (not read back in the DB's session tz).
    from datetime import UTC, datetime

    from nimbleship.domain.tracking import parse_voila

    [event] = parse_voila(
        {
            "tracking_update": {
                "shipment": {"reference": "ORD-1"},
                "parcels": [
                    {
                        "tracking_events": [
                            {
                                "status_code": 7,
                                "update_id": "E1",
                                "update_date": "2026-07-18T09:00:00",
                            }
                        ]
                    }
                ],
            }
        }
    )

    assert event.event_at == datetime(2026, 7, 18, 9, 0, tzinfo=UTC)


def test_an_out_of_vocabulary_status_is_rejected_before_storage(app: FastAPI) -> None:
    # ADR 0014's canonical vocabulary is closed; a mapping typo must fault (422),
    # never persist.
    from nimbleship.domain.tracking import ParsedTrackingEvent, TrackingError, ingest

    bad = ParsedTrackingEvent(
        order_number="ORD-1",
        external_id="E1",
        raw_status="4",
        status="in_transitt",  # a typo: not a member of TRACKING_STATUSES
        source_shipment_id=None,
        tracking_code=None,
        event_at=None,
        raw={},
    )
    with app.state.session_factory() as session:
        with pytest.raises(TrackingError, match="canonical tracking status"):
            ingest(session, "voila", [bad])
        assert session.execute(select(TrackingEvent)).scalars().all() == []


def test_a_mixed_batch_with_one_bad_event_stores_nothing(app: FastAPI) -> None:
    # The pre-pass validates every event before any insert, so one bad event in a
    # delivery rejects the whole batch - no partial store, the same atomicity the
    # length guard has.
    from nimbleship.domain.tracking import ParsedTrackingEvent, TrackingError, ingest

    def event(external_id: str, status: str) -> ParsedTrackingEvent:
        return ParsedTrackingEvent(
            order_number="ORD-1",
            external_id=external_id,
            raw_status="7",
            status=status,
            source_shipment_id=None,
            tracking_code=None,
            event_at=None,
            raw={},
        )

    batch = [event("E1", "delivered"), event("E2", "bogus")]
    with app.state.session_factory() as session:
        with pytest.raises(TrackingError):
            ingest(session, "voila", batch)
        assert session.execute(select(TrackingEvent)).scalars().all() == []


def test_a_present_but_falsy_shipment_id_or_tracking_code_is_kept() -> None:
    # 0 and "" are values the source sent, not absence: coercing them to None
    # would silently drop a real id. Only a genuinely missing key is None.
    from nimbleship.domain.tracking import parse_voila

    [event] = parse_voila(
        {
            "tracking_update": {
                "shipment_id": 0,
                "shipment": {"reference": "ORD-1"},
                "parcels": [
                    {
                        "tracking_code": "",
                        "tracking_events": [
                            {"status_code": 7, "update_id": "E1", "update_date": None}
                        ],
                    }
                ],
            }
        }
    )

    assert event.source_shipment_id == "0"
    assert event.tracking_code == ""


def test_a_non_scalar_shipment_id_or_tracking_code_collapses_to_none() -> None:
    # A malformed payload sending an object/array where a scalar id belongs must
    # not persist its Python repr ("[]"/"{}"); it is treated as absent, the same
    # silent-garbage class the status vocabulary guard closes for status.
    from nimbleship.domain.tracking import parse_voila

    [event] = parse_voila(
        {
            "tracking_update": {
                "shipment_id": [],
                "shipment": {"reference": "ORD-1"},
                "parcels": [
                    {
                        "tracking_code": {},
                        "tracking_events": [
                            {"status_code": 7, "update_id": "E1", "update_date": None}
                        ],
                    }
                ],
            }
        }
    )

    assert event.source_shipment_id is None
    assert event.tracking_code is None


def test_reading_an_orders_tracking_orders_by_carrier_event_time(
    app: FastAPI, client: TestClient, voila_secret: str
) -> None:
    # The delivered event (14:30) is listed BEFORE the in_transit one (09:00) in
    # the payload, so a chronological read proves it orders by event_at, not by
    # arrival order; current_status is the latest event's canonical status.
    payload = _voila_payload(
        events=[
            {
                "status_code": 7,
                "update_id": "EV-2",
                "update_date": "2026-07-18T14:30:00",
            },
            {
                "status_code": 4,
                "update_id": "EV-1",
                "update_date": "2026-07-18T09:00:00",
            },
        ]
    )
    client.post(
        "/api/tracking/webhooks/voila",
        json=payload,
        headers={"X-Webhook-Secret": voila_secret},
    )

    response = client.get("/api/tracking/95000254580")

    assert response.status_code == 200
    body = response.json()
    assert body["order_number"] == "95000254580"
    assert body["current_status"] == "delivered"
    assert [(e["status"], e["raw_status"]) for e in body["events"]] == [
        ("in_transit", "4"),
        ("delivered", "7"),
    ]
    first = body["events"][0]
    assert first["source"] == "voila"
    assert first["tracking_code"] == "TRK-1"
    assert first["event_at"].startswith("2026-07-18T09:00:00")
    assert first["received_at"] is not None
    # The raw payload and the internal idempotency key are not exposed.
    assert "raw" not in first
    assert "external_id" not in first


def test_reading_an_untracked_order_is_200_with_no_events(client: TestClient) -> None:
    # Tracking arrives asynchronously, so "no events yet" is a normal state, not a
    # 404 - and the store has no order registry to call an order "unknown".
    response = client.get("/api/tracking/NOPE-404")

    assert response.status_code == 200
    assert response.json() == {
        "order_number": "NOPE-404",
        "current_status": None,
        "events": [],
    }


def test_an_event_without_a_carrier_timestamp_is_still_returned(
    app: FastAPI, client: TestClient, voila_secret: str
) -> None:
    # event_at is null when the source omits it; the event still reads back (it
    # orders by received_at) with a null event_at and a set current_status.
    payload = _voila_payload(
        events=[{"status_code": 4, "update_id": "EV-1", "update_date": None}]
    )
    client.post(
        "/api/tracking/webhooks/voila",
        json=payload,
        headers={"X-Webhook-Secret": voila_secret},
    )

    body = client.get("/api/tracking/95000254580").json()

    assert body["current_status"] == "in_transit"
    [event] = body["events"]
    assert event["event_at"] is None
    assert event["received_at"] is not None


def test_reading_an_order_merges_events_across_sources(
    app: FastAPI, client: TestClient
) -> None:
    # ADR 0014: one order can be tracked by several sources. The read merges them
    # into one event_at-ordered timeline, with current_status from the latest
    # event overall - not partitioned by source.
    from datetime import UTC, datetime

    with app.state.session_factory() as session:
        session.add_all(
            [
                TrackingEvent(
                    order_number="MULTI-1",
                    source="voila",
                    external_id="V1",
                    raw_status="7",
                    status="delivered",
                    event_at=datetime(2026, 7, 18, 14, 30, tzinfo=UTC),
                    raw={},
                ),
                TrackingEvent(
                    order_number="MULTI-1",
                    source="othercarrier",
                    external_id="O1",
                    raw_status="MOVING",
                    status="in_transit",
                    event_at=datetime(2026, 7, 18, 9, 0, tzinfo=UTC),
                    raw={},
                ),
            ]
        )
        session.commit()

    body = client.get("/api/tracking/MULTI-1").json()

    assert [(e["source"], e["status"]) for e in body["events"]] == [
        ("othercarrier", "in_transit"),
        ("voila", "delivered"),
    ]
    assert body["current_status"] == "delivered"


def test_current_status_prefers_the_terminal_status_on_a_same_instant_tie(
    app: FastAPI, client: TestClient, voila_secret: str
) -> None:
    # Two events at the same instant: delivered is listed FIRST, exception second,
    # so "last event wins" would report exception. current_status must instead
    # prefer the more-advanced status - a delivery is not hidden by a same-second
    # exception.
    payload = _voila_payload(
        events=[
            {
                "status_code": 7,
                "update_id": "DEL",
                "update_date": "2026-07-18T09:00:00",
            },
            {
                "status_code": 8,
                "update_id": "EXC",
                "update_date": "2026-07-18T09:00:00",
            },
        ]
    )
    client.post(
        "/api/tracking/webhooks/voila",
        json=payload,
        headers={"X-Webhook-Secret": voila_secret},
    )

    body = client.get("/api/tracking/95000254580").json()

    assert body["current_status"] == "delivered"


def test_current_status_normalises_a_naive_and_aware_timestamp_mix() -> None:
    # A future adapter could yield a naive event_at alongside an aware one (Voila
    # pins tz today); current_status must normalise, not raise comparing them.
    from datetime import UTC, datetime

    from nimbleship.domain.tracking import current_status

    def event(status: str, when: datetime) -> TrackingEvent:
        return TrackingEvent(
            order_number="O",
            source="s",
            external_id=status,
            raw_status="x",
            status=status,
            event_at=when,
            received_at=when,
            raw={},
        )

    aware = event("delivered", datetime(2026, 7, 18, 14, 0, tzinfo=UTC))
    naive = event("in_transit", datetime(2026, 7, 18, 9, 0))  # no tzinfo

    assert current_status([aware, naive]) == "delivered"
