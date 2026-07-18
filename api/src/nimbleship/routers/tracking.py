"""Tracking webhooks (ADR 0014): a source POSTs tracking updates here; the
source's adapter normalises them into the Tracking Event store. Closed until the
source's webhook secret is configured - never open by omission, like the legacy
edge."""

import secrets
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nimbleship.config import get_settings
from nimbleship.db import get_session
from nimbleship.domain.tracking import (
    SOURCE_ADAPTERS,
    TrackingError,
    current_status,
    ingest,
)
from nimbleship.models import TrackingEvent

router = APIRouter(prefix="/tracking", tags=["tracking"])

SessionDep = Annotated[Session, Depends(get_session)]


def _source_secret(source: str) -> str | None:
    # A known source's secret is settings.<source>_webhook_secret, kept generic
    # so a new source names itself in config, not here. Runs before the adapter
    # lookup, so source is still the raw path value (unknown sources included).
    secret = getattr(get_settings(), f"{source}_webhook_secret", None)
    return secret if isinstance(secret, str) else None


@router.post("/webhooks/{source}", status_code=200)
def receive_tracking_webhook(
    source: str,
    payload: dict[str, object],
    session: SessionDep,
    x_webhook_secret: Annotated[str | None, Header()] = None,
) -> dict[str, int]:
    # Auth before the source lookup: an unknown source has no configured secret,
    # so it falls to the same 401 as a bad secret rather than a 404 that would
    # let an unauthenticated caller enumerate which sources exist. Constant-time
    # compared, and closed until configured.
    expected = _source_secret(source)
    if (
        expected is None
        or x_webhook_secret is None
        or not secrets.compare_digest(x_webhook_secret, expected)
    ):
        raise HTTPException(401, "invalid or missing webhook secret")
    adapter = SOURCE_ADAPTERS.get(source)
    if adapter is None:
        raise HTTPException(404, f"unknown tracking source '{source}'")
    try:
        events = adapter(payload)
        stored = ingest(session, source, events)
    except TrackingError as error:
        raise HTTPException(422, str(error)) from error
    return {"events_stored": stored}


class TrackingEventOut(BaseModel):
    source: str
    status: str
    raw_status: str
    tracking_code: str | None
    # When the carrier says it happened; None if the source omitted it.
    event_at: datetime | None
    # When this system ingested it - always present, the ordering fallback.
    received_at: datetime


class OrderTrackingOut(BaseModel):
    order_number: str
    # The canonical status of the most recent event, or None if untracked.
    current_status: str | None
    events: list[TrackingEventOut]


@router.get("/{order_number}")
def order_tracking(order_number: str, session: SessionDep) -> OrderTrackingOut:
    # Orders by event_at (falling back to received_at) for a real timeline; an
    # untracked order is 200/empty, not 404 - there's no order registry to check.
    events = (
        session.execute(
            select(TrackingEvent)
            .where(TrackingEvent.order_number == order_number)
            .order_by(
                func.coalesce(TrackingEvent.event_at, TrackingEvent.received_at),
                TrackingEvent.id,
            )
        )
        .scalars()
        .all()
    )
    out = [
        TrackingEventOut(
            source=event.source,
            status=event.status,
            raw_status=event.raw_status,
            tracking_code=event.tracking_code,
            event_at=event.event_at,
            received_at=event.received_at,
        )
        for event in events
    ]
    return OrderTrackingOut(
        order_number=order_number,
        # Not simply the last event's status: on an event_at tie the more-advanced
        # status wins, so a delivery is not hidden by a same-instant exception.
        current_status=current_status(events),
        events=out,
    )
