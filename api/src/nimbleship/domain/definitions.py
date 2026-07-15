"""Carrier Definition storage rails (ADR 0009 on the ADR 0003 pattern).

Mirrors the rulebook rails deliberately, including the concurrency
hardening those rails earned through review: publish is a guarded UPDATE
(atomic on every engine), serialised on Postgres by the same advisory-lock
approach, and seeding is double-checked so the hot path never takes the
global lock."""

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.models import CarrierConfig, CarrierDefinitionVersion

# Drop Out: the carrier-less route - NimbleShip renders the paperwork
# itself. The first Carrier Definition, replacing the walking skeleton's
# hardcoded path.
_DROPOUT_DEFINITION: dict[str, object] = {
    "carrier": "dropout",
    "name": "Drop Out",
    "auth": {"scheme": "none"},
    "operations": {
        "book": {
            "steps": [],
            "label": {"source": "local_render", "template": "standard_a6"},
        }
    },
}

# Key registered in the advisory-lock list in nimbleship/db.py.
_DEFINITIONS_LOCK_KEY = 815_005


def _serialise_definition_writes(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(_DEFINITIONS_LOCK_KEY)))


def _seed_dropout_if_fresh(session: Session) -> None:
    exists = session.execute(
        select(CarrierDefinitionVersion.id)
        .where(CarrierDefinitionVersion.carrier == "dropout")
        .limit(1)
    ).scalar_one_or_none()
    if exists is not None:
        return
    _serialise_definition_writes(session)
    exists = session.execute(
        select(CarrierDefinitionVersion.id)
        .where(CarrierDefinitionVersion.carrier == "dropout")
        .limit(1)
    ).scalar_one_or_none()
    if exists is None:
        session.add(
            CarrierDefinitionVersion(
                carrier="dropout",
                version=1,
                status="published",
                author="seed",
                data=_DROPOUT_DEFINITION,
            )
        )
        session.flush()


def definition_for(row: CarrierDefinitionVersion) -> CarrierDefinition:
    # A stored row was already vetted at publish: load leniently so tightening
    # an authoring-policy rule cannot strand a live definition. See
    # CarrierDefinition.load.
    return CarrierDefinition.load(row.data)


def active_definition_row(
    session: Session, carrier: str
) -> CarrierDefinitionVersion | None:
    _seed_dropout_if_fresh(session)
    return session.execute(
        select(CarrierDefinitionVersion)
        .where(
            CarrierDefinitionVersion.carrier == carrier,
            CarrierDefinitionVersion.status == "published",
        )
        .order_by(CarrierDefinitionVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()


def active_definition(session: Session, carrier: str) -> CarrierDefinition | None:
    row = active_definition_row(session, carrier)
    return definition_for(row) if row is not None else None


def list_versions(session: Session, carrier: str) -> list[CarrierDefinitionVersion]:
    _seed_dropout_if_fresh(session)
    return list(
        session.execute(
            select(CarrierDefinitionVersion)
            .where(CarrierDefinitionVersion.carrier == carrier)
            .order_by(CarrierDefinitionVersion.version)
        ).scalars()
    )


def get_version(
    session: Session, carrier: str, version: int
) -> CarrierDefinitionVersion | None:
    _seed_dropout_if_fresh(session)
    return session.execute(
        select(CarrierDefinitionVersion).where(
            CarrierDefinitionVersion.carrier == carrier,
            CarrierDefinitionVersion.version == version,
        )
    ).scalar_one_or_none()


def create_draft(
    session: Session, definition: CarrierDefinition, author: str
) -> CarrierDefinitionVersion:
    _seed_dropout_if_fresh(session)
    _serialise_definition_writes(session)
    latest = session.execute(
        select(func.max(CarrierDefinitionVersion.version)).where(
            CarrierDefinitionVersion.carrier == definition.carrier
        )
    ).scalar_one_or_none()
    row = CarrierDefinitionVersion(
        carrier=definition.carrier,
        version=(latest or 0) + 1,
        status="draft",
        author=author,
        data=definition.model_dump(mode="json", by_alias=True),
    )
    session.add(row)
    session.flush()
    return row


def publish(
    session: Session, row: CarrierDefinitionVersion
) -> CarrierDefinitionVersion:
    """Same shape as the rulebook's publish: refuse non-drafts and stale
    drafts, then claim the transition with a guarded UPDATE - atomic on
    every engine."""
    _serialise_definition_writes(session)
    session.refresh(row)
    if row.status != "draft":
        raise ValueError(f"version {row.version} is {row.status}, not a draft")
    live = session.execute(
        select(func.max(CarrierDefinitionVersion.version)).where(
            CarrierDefinitionVersion.carrier == row.carrier,
            CarrierDefinitionVersion.status == "published",
        )
    ).scalar_one_or_none()
    if live is not None and row.version < live:
        raise ValueError(
            f"version {row.version} would not become live: "
            f"version {live} is already published"
        )
    claimed: CursorResult[object] = session.execute(  # type: ignore[assignment]
        update(CarrierDefinitionVersion)
        .where(
            CarrierDefinitionVersion.id == row.id,
            CarrierDefinitionVersion.status == "draft",
        )
        .values(status="published")
    )
    if claimed.rowcount != 1:
        raise ValueError(f"version {row.version} was published by a concurrent request")
    session.refresh(row)
    return row


def carrier_config(session: Session, carrier: str) -> dict[str, object]:
    row = session.get(CarrierConfig, carrier)
    return row.data if row is not None else {}


def upsert_carrier_config(
    session: Session, carrier: str, data: dict[str, object]
) -> None:
    row = session.get(CarrierConfig, carrier)
    if row is None:
        session.add(CarrierConfig(carrier=carrier, data=data))
    else:
        row.data = data
    session.flush()
