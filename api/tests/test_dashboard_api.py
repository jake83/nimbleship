"""The dashboard's shipping-stats read: KPIs, bucketed volume and success/failure,
and the manifest-queue snapshot, aggregated from what the domain already records
(consignments, carrier traffic, manifests) - no dedicated stats tables."""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nimbleship.models import CarrierTraffic, Consignment, Manifest


def _consignment(order: str, carrier: str | None, created_at: datetime) -> Consignment:
    return Consignment(
        order_number=order,
        recipient_name="Jane Doe",
        address_lines=["1 High Street"],
        postcode="LS1 1AA",
        destination_country="GB",
        status="allocated" if carrier is not None else "received",
        carrier=carrier,
        allocation={},
        created_at=created_at,
    )


def _traffic(carrier: str, status: int | None, created_at: datetime) -> CarrierTraffic:
    return CarrierTraffic(
        carrier=carrier,
        order_number="95000000001",
        step="book",
        request={},
        response_status=status,
        response_body="",
        created_at=created_at,
    )


def _seed(app: FastAPI) -> None:
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    last_week = now - timedelta(days=10)
    with app.state.session_factory() as session:
        session.add_all(
            [
                _consignment("95000000001", "acme", now),
                _consignment("95000000002", "acme", now),
                _consignment("95000000003", "other", now),
                _consignment("95000000004", None, now),
                _consignment("95000000005", "acme", yesterday),
                # Outside every range but "1m"; must not leak into 7d numbers.
                _consignment("95000000006", "acme", last_week),
                _traffic("acme", 200, now),
                _traffic("acme", 500, now),
                _traffic("acme", None, yesterday),
                Manifest(carrier="acme", status="pending"),
                Manifest(carrier="acme", status="failed", attempts=3),
                Manifest(carrier="acme", status="sent", sent_at=now),
            ]
        )
        session.commit()


def test_stats_aggregate_kpis_volume_and_the_manifest_queue(
    app: FastAPI, client: TestClient
) -> None:
    _seed(app)
    response = client.get("/api/dashboard/shipping-stats?range=7d")
    assert response.status_code == 200
    body = response.json()

    assert body["range"] == "7d"
    kpis = body["kpis"]
    assert kpis["consignments_today"] == 4
    assert kpis["consignments_yesterday"] == 1
    assert kpis["failures_today"] == 1  # the 500; the None-status call was yesterday
    # 1 success of 3 calls in the window.
    assert kpis["success_rate_7d"] == 33.3
    assert kpis["busiest_carrier_7d"] == {"carrier": "acme", "count": 3}

    # Daily buckets, oldest first, today last.
    assert len(body["buckets"]) == 7
    volume = {series["carrier"]: series["data"] for series in body["volume"]}
    assert volume["acme"][-1] == 2
    assert volume["acme"][-2] == 1  # yesterday
    assert volume["other"][-1] == 1
    assert volume["unallocated"][-1] == 1
    assert sum(volume["acme"]) == 3  # the 10-day-old row stays outside

    assert body["success_failure"]["success"][-1] == 1
    assert body["success_failure"]["failed"][-1] == 1
    assert body["success_failure"]["failed"][-2] == 1  # yesterday's never-reached call

    assert body["manifest_queue"] == {"pending": 1, "failed": 1, "sent_today": 1}


def test_stats_today_uses_hourly_buckets(app: FastAPI, client: TestClient) -> None:
    _seed(app)
    body = client.get("/api/dashboard/shipping-stats?range=today").json()
    now = datetime.now(UTC)
    assert len(body["buckets"]) == now.hour + 1  # midnight through the current hour
    volume = {series["carrier"]: series["data"] for series in body["volume"]}
    assert sum(volume["acme"]) == 2  # yesterday's rows fall outside

    assert client.get("/api/dashboard/shipping-stats?range=1m").json()["buckets"] != []
    assert client.get("/api/dashboard/shipping-stats?range=bogus").status_code == 422


def test_stats_are_empty_not_broken_on_a_fresh_install(client: TestClient) -> None:
    body = client.get("/api/dashboard/shipping-stats?range=7d").json()
    assert body["kpis"]["consignments_today"] == 0
    assert body["kpis"]["success_rate_7d"] is None  # no calls: no rate, not 0%
    assert body["kpis"]["busiest_carrier_7d"] is None
    assert body["volume"] == []
    assert body["manifest_queue"] == {"pending": 0, "failed": 0, "sent_today": 0}
