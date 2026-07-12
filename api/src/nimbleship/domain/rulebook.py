"""Versioned rulebook storage and workflow (ADR 0003).

Versions are immutable rows: drafts are created, dry-run tested, and
published - never edited. The highest published version is live."""

from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.domain.allocation import Rulebook, ServiceDeclaration
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


def create_draft(
    session: Session, services: list[ServiceDeclaration], author: str
) -> RulebookVersion:
    """Create an immutable draft version. Validation (unique codes and
    tie-break orders) happens by constructing the Rulebook model before
    anything is stored; the version number is only meaningful once saved."""
    _seed_if_fresh(session)
    Rulebook(version=0, services=services)
    row = RulebookVersion(
        status="draft",
        author=author,
        data={"services": [s.model_dump(mode="json") for s in services]},
    )
    session.add(row)
    session.flush()
    return row


def publish(session: Session, row: RulebookVersion) -> RulebookVersion:
    """Publish a draft. Rows are immutable except for this one transition;
    the newly published version becomes live because highest-published wins.
    Publishing a draft older than the live version is refused: it would
    "succeed" while silently changing nothing. Rolling back means drafting
    a new version with the old content, keeping history linear."""
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
    row.status = "published"
    session.flush()
    return row
