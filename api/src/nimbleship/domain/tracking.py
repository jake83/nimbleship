"""Tracking Events (CONTEXT.md): carrier tracking signals ingested from a
source webhook into a dedicated store. Each source has an adapter that parses
its payload and normalises its raw status codes onto the canonical vocabulary;
ingestion is idempotent on (source, external_id) so a redelivered webhook is a
no-op. A raw code with no mapping lands as "unknown", never silently dropped."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nimbleship.models import (
    ORDER_NUMBER_MAX,
    TRACKING_ID_MAX,
    TRACKING_RAW_STATUS_MAX,
    TrackingEvent,
)

# The canonical status a raw carrier code normalises to. Small and
# carrier-neutral; a code with no mapping becomes "unknown".
TRACKING_STATUSES = frozenset(
    {
        "in_transit",
        "out_for_delivery",
        "delivered",
        "exception",
        "returned",
        "unknown",
    }
)


class TrackingError(Exception):
    """A malformed tracking payload; each edge maps it to its own error shape."""


@dataclass
class ParsedTrackingEvent:
    order_number: str
    external_id: str
    raw_status: str
    status: str
    source_shipment_id: str | None
    tracking_code: str | None
    event_at: datetime | None
    raw: dict[str, object]


def _reject_long(label: str, value: str | None, limit: int) -> None:
    if value is not None and len(value) > limit:
        raise TrackingError(f"{label} exceeds {limit} characters")


def ingest(session: Session, source: str, events: list[ParsedTrackingEvent]) -> int:
    """Store the parsed events, skipping any already seen for this source, and
    return how many were new. Idempotency is the (source, external_id) unique
    constraint, enforced per event in a savepoint: a redelivery - including two
    concurrent ones racing the same event - resolves to a skip, never a 500.

    Over-length source fields are rejected up front (TrackingError -> 422): on
    Postgres a VARCHAR overflow is a driver DataError the savepoint's
    IntegrityError catch would miss, so it must not reach the column."""
    for event in events:
        _reject_long("order number", event.order_number, ORDER_NUMBER_MAX)
        _reject_long("external id", event.external_id, TRACKING_ID_MAX)
        _reject_long("shipment id", event.source_shipment_id, TRACKING_ID_MAX)
        _reject_long("tracking code", event.tracking_code, TRACKING_ID_MAX)
        _reject_long("raw status", event.raw_status, TRACKING_RAW_STATUS_MAX)
    stored = 0
    for event in events:
        try:
            with session.begin_nested():
                session.add(
                    TrackingEvent(
                        order_number=event.order_number,
                        source=source,
                        external_id=event.external_id,
                        source_shipment_id=event.source_shipment_id,
                        tracking_code=event.tracking_code,
                        raw_status=event.raw_status,
                        status=event.status,
                        event_at=event.event_at,
                        raw=event.raw,
                    )
                )
                session.flush()
        except IntegrityError:
            continue
        stored += 1
    return stored


# Voila's numeric status codes -> canonical. Pre-delivery movement collapses to
# in_transit; delivery problems to exception. Reviewed against the source's own
# code table; the per-code mapping is source config, not a hard contract.
_VOILA_STATUS: dict[str, str] = {
    "1": "in_transit",  # Booked
    "2": "in_transit",  # Collected
    "3": "in_transit",  # At Hub
    "4": "in_transit",  # In Transit
    "14": "in_transit",  # Packed
    "5": "out_for_delivery",  # Out For Delivery
    "13": "out_for_delivery",  # Awaiting Customer Collection
    "7": "delivered",  # Delivered
    "10": "returned",  # Returned To Sender
    "6": "exception",  # Failed Attempt
    "8": "exception",  # On Hold
    "9": "exception",  # Address Issue
    "11": "exception",  # Tracking Expired
    "12": "exception",  # Cancelled
    "15": "exception",  # Missing
    "16": "exception",  # Damaged
    "18": "exception",  # CustomsHold
    "103": "exception",  # Authentication Failed
    "17": "unknown",  # Ignored
}


def _voila_event_at(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    # A source timestamp without an offset is UTC; pin it so the tz-aware column
    # stores an unambiguous instant, not a value read back in the DB's session tz.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def parse_voila(payload: Mapping[str, object]) -> list[ParsedTrackingEvent]:
    """Parse a Voila tracking webhook into normalised events. The order is the
    shipment reference; each parcel carries its own tracking events keyed by the
    source's update_id. An event missing its status code or update_id is skipped
    (the source sends partial rows), not faulted."""
    update = _mapping(payload.get("tracking_update"))
    if update is None:
        raise TrackingError("voila: no tracking_update")
    shipment = _mapping(update.get("shipment")) or {}
    order_number = shipment.get("reference")
    if not isinstance(order_number, str) or not order_number:
        raise TrackingError("voila: tracking_update.shipment.reference is missing")
    shipment_id = update.get("shipment_id")
    parcels = update.get("parcels")
    events: list[ParsedTrackingEvent] = []
    for parcel in parcels if isinstance(parcels, list) else []:
        parcel_map = _mapping(parcel) or {}
        tracking_code = parcel_map.get("tracking_code")
        raw_events = parcel_map.get("tracking_events")
        for raw_event in raw_events if isinstance(raw_events, list) else []:
            event_map = _mapping(raw_event)
            if event_map is None:
                continue
            status_code = event_map.get("status_code")
            update_id = event_map.get("update_id")
            if status_code is None or update_id is None:
                continue
            raw_status = str(status_code)
            events.append(
                ParsedTrackingEvent(
                    order_number=order_number,
                    external_id=str(update_id),
                    raw_status=raw_status,
                    status=_VOILA_STATUS.get(raw_status, "unknown"),
                    source_shipment_id=str(shipment_id) if shipment_id else None,
                    tracking_code=str(tracking_code) if tracking_code else None,
                    event_at=_voila_event_at(event_map.get("update_date")),
                    raw=dict(event_map),
                )
            )
    return events


def _mapping(value: object) -> Mapping[str, object] | None:
    return value if isinstance(value, Mapping) else None


# The source-adapter seam: a webhook for a source dispatches to its parser. New
# sources (direct-carrier feeds) register here without touching the endpoint.
Adapter = Callable[[Mapping[str, object]], list[ParsedTrackingEvent]]
SOURCE_ADAPTERS: dict[str, Adapter] = {
    "voila": parse_voila,
}
