"""Versioned rulebook storage and workflow (ADR 0003).

Versions are immutable rows: drafts are created, dry-run tested, and
published - never edited. The highest published version is live."""

from typing import cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from nimbleship.domain.allocation import Rulebook, ServiceDeclaration
from nimbleship.domain.propositions import known_proposition_codes
from nimbleship.domain.service_groups import known_service_group_codes
from nimbleship.models import RulebookVersion

# Demo seed for fresh installs: two generic Drop Out services proving the
# weight-band and country declarations plus cheapest-cost selection.
# Real installs replace this via the rules workflow - never in code.
_DEMO_SERVICES: list[dict[str, object]] = [
    {
        "code": "DROPOUT-STD",
        "carrier": "dropout",
        "name": "Drop Out Standard",
        "weight_min_kg": "0",
        "weight_max_kg": "30",
        "countries": ["GB"],
        "cost": "4.50",
        "tie_break_order": 1,
        # Member of the ECONOMY group so the legacy path (custom1=ECONOMY)
        # allocates end to end (ADR 0012).
        "service_groups": ["ECONOMY"],
    },
    {
        "code": "DROPOUT-XL",
        "carrier": "dropout",
        "name": "Drop Out Extra Large",
        "weight_min_kg": "0",
        "weight_max_kg": "999",
        "countries": ["GB", "IE", "FR"],
        "cost": "12.00",
        "tie_break_order": 2,
        "service_groups": ["ECONOMY"],
    },
]


def rulebook_for(row: RulebookVersion) -> Rulebook:
    declared = cast(list[dict[str, object]], row.data["services"])
    services = [ServiceDeclaration.model_validate(service) for service in declared]
    return Rulebook(version=row.version, services=services)


def _seed_if_fresh(session: Session) -> None:
    """Seed when NO row exists at all. Invariant this relies on: every
    public entry point in this module seeds before drafts can be created,
    so a published version always exists whenever any row exists - which is
    what lets active_rulebook() use scalar_one(). If an entry point ever
    skips seeding, restore a status == "published" check here."""
    exists = session.execute(
        select(RulebookVersion.version).limit(1)
    ).scalar_one_or_none()
    if exists is not None:
        return
    # Double-checked locking: this path runs on every rulebook read,
    # including the order-creation hot path, so the (transaction-scoped,
    # global) lock is taken only in the once-per-install case where a seed
    # insert may actually happen - then the check repeats under the lock
    # (reviewer, PR #9). On SQLite the lock is a no-op and two concurrent
    # first-ever requests can double-seed; that is accepted as benign (the
    # rows are identical demo data and highest-published wins) rather than
    # falsely claimed prevented (refuter, PR #9).
    _serialise_rulebook_writes(session)
    exists = session.execute(
        select(RulebookVersion.version).limit(1)
    ).scalar_one_or_none()
    if exists is None:
        session.add(
            RulebookVersion(
                status="published",
                author="seed",
                data={"services": _DEMO_SERVICES},
            )
        )
        session.flush()


def active_rulebook(session: Session) -> Rulebook:
    """The highest published rulebook version; seeds the demo rulebook on a
    fresh install."""
    _seed_if_fresh(session)
    row = session.execute(
        select(RulebookVersion)
        .where(RulebookVersion.status == "published")
        .order_by(RulebookVersion.version.desc())
        .limit(1)
    ).scalar_one()
    return rulebook_for(row)


def list_versions(session: Session) -> list[RulebookVersion]:
    _seed_if_fresh(session)
    return list(
        session.execute(
            select(RulebookVersion).order_by(RulebookVersion.version)
        ).scalars()
    )


def get_version(session: Session, version: int) -> RulebookVersion | None:
    _seed_if_fresh(session)
    return session.get(RulebookVersion, version)


def description_of(row: RulebookVersion) -> str | None:
    """The version's optional rationale note (ADR 0017), stored in the data blob -
    provenance an operator reads instead of reverse-engineering the diff."""
    value = (row.data or {}).get("description")
    return value if isinstance(value, str) else None


def create_draft(
    session: Session,
    services: list[ServiceDeclaration],
    author: str,
    description: str | None = None,
) -> RulebookVersion:
    """Create an immutable draft version. Validation (unique codes and
    tie-break orders) happens by constructing the Rulebook model before
    anything is stored; the version number is only meaningful once saved.
    Proposition references are checked against the catalogue here, at
    authoring time, so a typo fails the author instead of silently never
    matching any shipment at allocation time. An optional description records
    why the version exists (ADR 0017)."""
    _seed_if_fresh(session)
    Rulebook(version=0, services=services)
    named = {code for service in services for code in service.propositions}
    unknown = named - known_proposition_codes(session)
    if unknown:
        raise ValueError("unknown proposition codes: " + ", ".join(sorted(unknown)))
    named_groups = {code for service in services for code in service.service_groups}
    unknown_groups = named_groups - known_service_group_codes(session)
    if unknown_groups:
        raise ValueError(
            "unknown service group codes: " + ", ".join(sorted(unknown_groups))
        )
    data: dict[str, object] = {
        "services": [s.model_dump(mode="json") for s in services]
    }
    # Normalise here, at the one write path, so a blank or whitespace note never
    # persists: versions are immutable, so a stored "" would be a permanent empty
    # line no edit could remove.
    note = description.strip() if description is not None else ""
    if note:
        data["description"] = note
    row = RulebookVersion(status="draft", author=author, data=data)
    session.add(row)
    session.flush()
    return row


# Arbitrary app-wide advisory lock key for rulebook writes (seed, publish).
_RULEBOOK_LOCK_KEY = 815_003


def _serialise_rulebook_writes(session: Session) -> None:
    """Advisory serialisation of rulebook writers on Postgres. This is
    belt-and-braces around the atomic guarded UPDATE in publish(): the lock
    additionally makes the stale-draft (would-not-become-live) check exact
    under concurrency. On SQLite this is a no-op; there, the guarded UPDATE
    carries the correctness load alone (this branch is untested in CI, which
    runs SQLite only - verify against real Postgres before reusing the
    pattern elsewhere)."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(_RULEBOOK_LOCK_KEY)))


def publish(session: Session, row: RulebookVersion) -> RulebookVersion:
    """Publish a draft. Rows are immutable except for this one transition;
    the newly published version becomes live because highest-published wins.
    Publishing a draft older than the live version is refused: it would
    "succeed" while silently changing nothing. Rolling back means drafting
    a new version with the old content, keeping history linear."""
    _serialise_rulebook_writes(session)
    session.refresh(row)
    if row.status != "draft":
        raise ValueError(f"version {row.version} is {row.status}, not a draft")
    live = session.execute(
        select(RulebookVersion.version)
        .where(RulebookVersion.status == "published")
        .order_by(RulebookVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if live is not None and row.version < live:
        raise ValueError(
            f"version {row.version} would not become live: "
            f"version {live} is already published"
        )
    # The transition itself is a single guarded UPDATE: atomic on every
    # engine, so two racing publishes of the same draft cannot both win -
    # multi-statement check-then-act is NOT atomic under SQLite's
    # single-writer model, as the refuter proved with a barrier race.
    claimed: CursorResult[object] = session.execute(  # type: ignore[assignment]
        update(RulebookVersion)
        .where(
            RulebookVersion.version == row.version,
            RulebookVersion.status == "draft",
        )
        .values(status="published")
    )
    if claimed.rowcount != 1:
        raise ValueError(f"version {row.version} was published by a concurrent request")
    session.refresh(row)
    return row
