"""Read-only tools the AI assistant (ADR 0016) calls to answer single-order
diagnostics. Each queries the domain by order number and returns a structured,
JSON-serialisable result the model narrates - never a write. The failing checks in
allocation_trace carry the exact `expected`/`actual` so an answer names its reason."""

from collections.abc import Callable, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.domain.model import AllocationResult
from nimbleship.domain.tracking import current_status
from nimbleship.models import (
    Consignment,
    Manifest,
    ManifestConsignment,
    OrderEvent,
    TrackingEvent,
)


def _order_known(session: Session, order_number: str) -> bool:
    """Whether the order exists in the system at all - a consignment, an event, or a
    tracking row. Every tool reports it so the assistant can tell an unknown order (a
    typo) from a known order that just has nothing at that stage yet, and never guess
    which (ADR 0016 grounding). Tracking counts: a carrier webhook can post for an
    order with no consignment yet, and that tool returns its events - order_known
    must agree, not contradict them."""
    for table in (
        Consignment.order_number,
        OrderEvent.order_number,
        TrackingEvent.order_number,
    ):
        if (
            session.execute(select(table).where(table == order_number)).first()
            is not None
        ):
            return True
    return False


def order_timeline(session: Session, order_number: str) -> dict[str, object]:
    """The append-only order event timeline: what happened to the order, in order."""
    events = (
        session.execute(
            select(OrderEvent)
            .where(OrderEvent.order_number == order_number)
            .order_by(OrderEvent.created_at, OrderEvent.id)
        )
        .scalars()
        .all()
    )
    return {
        "order_number": order_number,
        "order_known": _order_known(session, order_number),
        "events": [
            {"stage": e.stage, "at": e.created_at.isoformat(), "detail": e.detail}
            for e in events
        ],
    }


def allocation_trace(session: Session, order_number: str) -> dict[str, object]:
    """Why this order allocated as it did: the selected service (if any) and reason,
    and for every candidate service which named checks failed with expected vs
    actual - the evidence behind 'X was excluded' or 'Y was chosen'."""
    consignment = session.execute(
        select(Consignment).where(Consignment.order_number == order_number)
    ).scalar_one_or_none()
    if consignment is None or consignment.allocation is None:
        return {
            "order_number": order_number,
            "found": False,
            "order_known": _order_known(session, order_number),
        }
    result = AllocationResult.model_validate(consignment.allocation)
    selected = result.selected
    return {
        "order_number": order_number,
        "found": True,
        "order_known": True,
        "rulebook_version": result.rulebook_version,
        "reason": result.reason,
        "selected": None
        if selected is None
        else {
            "carrier": selected.carrier,
            "service": selected.code,
            "cost": None if result.selected_cost is None else str(result.selected_cost),
        },
        "services": [
            {
                "service_code": sr.service_code,
                "eligible": sr.eligible,
                "failed_checks": [
                    {"name": c.name, "expected": c.expected, "actual": c.actual}
                    for c in sr.checks
                    if not c.ok
                ],
            }
            for sr in result.service_results
        ],
    }


def tracking(session: Session, order_number: str) -> dict[str, object]:
    """The order's carrier tracking: the canonical current status and every event."""
    events = (
        session.execute(
            select(TrackingEvent).where(TrackingEvent.order_number == order_number)
        )
        .scalars()
        .all()
    )
    return {
        "order_number": order_number,
        "order_known": _order_known(session, order_number),
        "current_status": current_status(events),
        "events": [
            {
                "status": e.status,
                "raw_status": e.raw_status,
                "at": (e.event_at or e.received_at).isoformat(),
            }
            for e in events
        ],
    }


def manifest_status(session: Session, order_number: str) -> dict[str, object]:
    """The manifest carrying the order's consignment, if any: its send state and the
    last error when a send failed."""
    consignment = session.execute(
        select(Consignment).where(Consignment.order_number == order_number)
    ).scalar_one_or_none()
    if consignment is None:
        return {
            "order_number": order_number,
            "found": False,
            "order_known": _order_known(session, order_number),
        }
    # A consignment is on at most one manifest (ADR 0013: a failed send parks for a
    # human, never re-manifests), so scalar_one_or_none is exact.
    manifest = session.execute(
        select(Manifest)
        .join(ManifestConsignment, ManifestConsignment.manifest_id == Manifest.id)
        .where(ManifestConsignment.consignment_id == consignment.id)
    ).scalar_one_or_none()
    if manifest is None:
        return {"order_number": order_number, "found": False, "order_known": True}
    return {
        "order_number": order_number,
        "found": True,
        "order_known": True,
        "carrier": manifest.carrier,
        "warehouse": manifest.warehouse,
        "status": manifest.status,
        "last_error": manifest.last_error,
        "sent_at": None if manifest.sent_at is None else manifest.sent_at.isoformat(),
    }


_TOOLS: dict[str, Callable[[Session, str], dict[str, object]]] = {
    "order_timeline": order_timeline,
    "allocation_trace": allocation_trace,
    "tracking": tracking,
    "manifest_status": manifest_status,
}


def _schema(name: str, description: str) -> dict[str, object]:
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": {
                "order_number": {
                    "type": "string",
                    "description": "The order number to diagnose.",
                }
            },
            "required": ["order_number"],
        },
    }


# The Anthropic tool definitions, one per read tool. Every tool is scoped to a
# single order number the model takes from the operator's question.
TOOL_SCHEMAS: Sequence[dict[str, object]] = (
    _schema("order_timeline", order_timeline.__doc__ or ""),
    _schema("allocation_trace", allocation_trace.__doc__ or ""),
    _schema("tracking", tracking.__doc__ or ""),
    _schema("manifest_status", manifest_status.__doc__ or ""),
)


def run_tool(
    session: Session, name: str, tool_input: dict[str, object]
) -> dict[str, object]:
    """Dispatch a model tool call to its read function. An unknown tool or a missing
    order number returns an error dict rather than raising, so the loop can hand it
    back to the model instead of crashing."""
    func = _TOOLS.get(name)
    if func is None:
        return {"error": f"unknown tool '{name}'"}
    order_number = tool_input.get("order_number")
    if not isinstance(order_number, str) or not order_number:
        return {"error": "order_number is required"}
    return func(session, order_number)
