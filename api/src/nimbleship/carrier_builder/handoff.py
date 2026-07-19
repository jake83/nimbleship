"""Handoff blockers (CONTEXT.md: Handoff; ADR 0018): the durable, carrier-keyed
record of technical gaps the builder parked for the engineer. The conversation that
raises one is ephemeral; the blocker outlives it (a plugin is a PR and a deploy, so
resolution is days later). A carrier with an open blocker cannot publish."""

from datetime import UTC, datetime

from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session

from nimbleship.models import HandoffBlocker

KINDS = ("needs_plugin", "needs_decision")

# Bounded to the columns, enforced here at the write path: the tool's input is a raw
# model-supplied dict, and SQLite (the test engine) would silently accept what
# Postgres rejects at flush as an uncaught DataError.
CARRIER_MAX = 64
TITLE_MAX = 255
PLUGIN_NAME_MAX = 64


class UnknownBlocker(ValueError):
    """No blocker with the given id."""


class BlockerAlreadyResolved(ValueError):
    """The blocker was already resolved; resolving again would silently overwrite
    the recorded answer."""


def raise_blocker(
    session: Session,
    carrier: str,
    kind: str,
    title: str,
    detail: str,
    plugin_name: str | None = None,
) -> HandoffBlocker:
    """Record a blocker for `carrier`. A needs_plugin blocker must name the plugin the
    definition will reference once the engineer ships it - enforced here, where the row
    is built, so no caller can create one without it."""
    if kind not in KINDS:
        raise ValueError(f"unknown blocker kind '{kind}'")
    if kind == "needs_plugin" and (plugin_name is None or not plugin_name.strip()):
        raise ValueError("a needs_plugin blocker must name the plugin to build")
    # A blank brief makes the queue entry and the publish-refusal message contentless,
    # and there is no edit path - only resolve. Text columns accept "" on both engines,
    # so the invariant lives here (refuter, PR #127).
    if not title.strip() or not detail.strip():
        raise ValueError("title and detail must not be blank")
    if len(carrier) > CARRIER_MAX:
        raise ValueError(f"carrier must be {CARRIER_MAX} characters or fewer")
    if len(title) > TITLE_MAX:
        raise ValueError(f"title must be {TITLE_MAX} characters or fewer")
    if plugin_name is not None and len(plugin_name) > PLUGIN_NAME_MAX:
        raise ValueError(f"plugin_name must be {PLUGIN_NAME_MAX} characters or fewer")
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
    blocker. A guarded UPDATE makes the open -> resolved transition atomic: two
    engineers racing the queue can't both win, so the loser's answer can't silently
    overwrite the recorded one (a plain read-then-write check could) - the loser gets
    BlockerAlreadyResolved; an unknown id gets UnknownBlocker."""
    won: CursorResult[object] = session.execute(  # type: ignore[assignment]
        update(HandoffBlocker)
        .where(HandoffBlocker.id == blocker_id, HandoffBlocker.status == "open")
        .values(
            status="resolved",
            resolution=resolution,
            resolved_at=datetime.now(UTC),
        )
    )
    if won.rowcount == 0:
        blocker = session.get(HandoffBlocker, blocker_id)
        if blocker is None:
            raise UnknownBlocker(f"no blocker {blocker_id}")
        raise BlockerAlreadyResolved(f"blocker {blocker_id} is already resolved")
    session.flush()
    resolved = session.get(HandoffBlocker, blocker_id)
    assert resolved is not None  # the UPDATE just matched this id
    session.refresh(resolved)
    return resolved
