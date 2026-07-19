"""Handoff blockers (CONTEXT.md: Handoff; ADR 0018): the durable, carrier-keyed
record of technical gaps the builder parked for the engineer. The conversation that
raises one is ephemeral; the blocker outlives it (a plugin is a PR and a deploy, so
resolution is days later). A carrier with an open blocker cannot publish."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.models import HandoffBlocker

KINDS = ("needs_plugin", "needs_decision")


def raise_blocker(
    session: Session,
    carrier: str,
    kind: str,
    title: str,
    detail: str,
    plugin_name: str | None = None,
) -> HandoffBlocker:
    """Record a blocker for `carrier`. `kind` must be a known kind; a needs_plugin
    blocker names the plugin the definition will reference once the engineer ships
    it."""
    if kind not in KINDS:
        raise ValueError(f"unknown blocker kind '{kind}'")
    blocker = HandoffBlocker(
        carrier=carrier,
        kind=kind,
        title=title,
        detail=detail,
        plugin_name=plugin_name,
    )
    session.add(blocker)
    session.flush()
    return blocker


def blockers_for(session: Session, carrier: str) -> list[HandoffBlocker]:
    """All of a carrier's blockers, open and resolved, oldest first - the engineer's
    queue and the builder's memory of what was parked and what came back answered."""
    return list(
        session.execute(
            select(HandoffBlocker)
            .where(HandoffBlocker.carrier == carrier)
            .order_by(HandoffBlocker.id)
        ).scalars()
    )


def open_blockers(session: Session, carrier: str) -> list[HandoffBlocker]:
    return [b for b in blockers_for(session, carrier) if b.status == "open"]


def resolve_blocker(
    session: Session, blocker_id: int, resolution: str
) -> HandoffBlocker:
    """Record the engineer's answer (a decision, or "shipped in vX") and close the
    blocker. Raises ValueError for an unknown or already-resolved blocker - resolving
    twice would silently overwrite the recorded answer."""
    blocker = session.get(HandoffBlocker, blocker_id)
    if blocker is None:
        raise ValueError(f"no blocker {blocker_id}")
    if blocker.status != "open":
        raise ValueError(f"blocker {blocker_id} is already resolved")
    blocker.status = "resolved"
    blocker.resolution = resolution
    blocker.resolved_at = datetime.now(UTC)
    session.flush()
    return blocker
