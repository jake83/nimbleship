"""The dashboard's shipping-stats read: aggregated on demand from what the domain
already records (consignments, carrier traffic, manifests) - no dedicated stats
tables, so the numbers can never drift from the records they summarise."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.db import get_session
from nimbleship.models import CarrierTraffic, Consignment, Manifest

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

SessionDep = Annotated[Session, Depends(get_session)]

StatsRange = Literal["today", "7d", "1m"]


class CarrierSeries(BaseModel):
    carrier: str
    data: list[int]


class SuccessFailure(BaseModel):
    success: list[int]
    failed: list[int]


class BusiestCarrier(BaseModel):
    carrier: str
    count: int


class Kpis(BaseModel):
    consignments_today: int
    consignments_yesterday: int
    failures_today: int
    # None when the window saw no carrier calls: an absent rate, not a 0% one.
    success_rate_7d: float | None
    busiest_carrier_7d: BusiestCarrier | None


class ManifestQueue(BaseModel):
    pending: int
    failed: int
    sent_today: int


class StatsOut(BaseModel):
    range: StatsRange
    kpis: Kpis
    buckets: list[str]
    volume: list[CarrierSeries]
    success_failure: SuccessFailure
    manifest_queue: ManifestQueue


def _aware(moment: datetime) -> datetime:
    # SQLite hands timezone-aware columns back naive; the values are stored UTC.
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _succeeded(status: int | None) -> bool:
    # None means the carrier was never reached (connect/timeout) - a failure.
    return status is not None and 200 <= status < 300


@router.get("/shipping-stats")
def shipping_stats(
    stats_range: Annotated[StatsRange, Query(alias="range")],
    session: SessionDep,
) -> StatsOut:
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if stats_range == "today":
        # Midnight through the current hour, so the last bucket is always "now".
        starts = [today + timedelta(hours=hour) for hour in range(now.hour + 1)]
        labels = [start.strftime("%H:00") for start in starts]
        step = timedelta(hours=1)
    else:
        days = 7 if stats_range == "7d" else 30
        starts = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        labels = [start.date().isoformat() for start in starts]
        step = timedelta(days=1)
    window_start = starts[0]

    def bucket_of(moment: datetime) -> int | None:
        offset = _aware(moment) - window_start
        index = int(offset / step)
        return index if 0 <= index < len(starts) else None

    volume: dict[str, list[int]] = {}
    for created_at, carrier in session.execute(
        select(Consignment.created_at, Consignment.carrier).where(
            Consignment.created_at >= window_start
        )
    ):
        index = bucket_of(created_at)
        if index is None:
            continue
        series = volume.setdefault(carrier or "unallocated", [0] * len(starts))
        series[index] += 1

    success = [0] * len(starts)
    failed = [0] * len(starts)
    for created_at, status in session.execute(
        select(CarrierTraffic.created_at, CarrierTraffic.response_status).where(
            CarrierTraffic.created_at >= window_start
        )
    ):
        index = bucket_of(created_at)
        if index is None:
            continue
        (success if _succeeded(status) else failed)[index] += 1

    yesterday = today - timedelta(days=1)
    consignments_today = 0
    consignments_yesterday = 0
    week_carriers: dict[str, int] = {}
    for created_at, carrier in session.execute(
        select(Consignment.created_at, Consignment.carrier).where(
            Consignment.created_at >= today - timedelta(days=6)
        )
    ):
        moment = _aware(created_at)
        if moment >= today:
            consignments_today += 1
        elif moment >= yesterday:
            consignments_yesterday += 1
        if carrier is not None:
            week_carriers[carrier] = week_carriers.get(carrier, 0) + 1

    calls_7d = 0
    successes_7d = 0
    failures_today = 0
    for created_at, status in session.execute(
        select(CarrierTraffic.created_at, CarrierTraffic.response_status).where(
            CarrierTraffic.created_at >= today - timedelta(days=6)
        )
    ):
        calls_7d += 1
        ok = _succeeded(status)
        if ok:
            successes_7d += 1
        elif _aware(created_at) >= today:
            failures_today += 1

    busiest = max(week_carriers.items(), key=lambda item: item[1], default=None)

    manifest_pending = 0
    manifest_failed = 0
    manifest_sent_today = 0
    for status, sent_at in session.execute(select(Manifest.status, Manifest.sent_at)):
        if status == "pending":
            manifest_pending += 1
        elif status == "failed":
            manifest_failed += 1
        elif status == "sent" and sent_at is not None and _aware(sent_at) >= today:
            manifest_sent_today += 1

    return StatsOut(
        range=stats_range,
        kpis=Kpis(
            consignments_today=consignments_today,
            consignments_yesterday=consignments_yesterday,
            failures_today=failures_today,
            success_rate_7d=(
                round(100 * successes_7d / calls_7d, 1) if calls_7d else None
            ),
            busiest_carrier_7d=(
                BusiestCarrier(carrier=busiest[0], count=busiest[1])
                if busiest is not None
                else None
            ),
        ),
        buckets=labels,
        volume=[
            CarrierSeries(carrier=carrier, data=data)
            for carrier, data in sorted(volume.items(), key=lambda item: -sum(item[1]))
        ],
        success_failure=SuccessFailure(success=success, failed=failed),
        manifest_queue=ManifestQueue(
            pending=manifest_pending,
            failed=manifest_failed,
            sent_today=manifest_sent_today,
        ),
    )
