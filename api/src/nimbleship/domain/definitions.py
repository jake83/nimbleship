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

# Keys registered in the advisory-lock list in nimbleship/db.py.
_DEFINITIONS_LOCK_KEY = 815_005
_CONFIG_LOCK_KEY = 815_007


def _advisory_xact_lock(session: Session, key: int) -> None:
    # Serialises writers on Postgres (the production engine): held to the end of
    # the transaction, so a read-modify-write under it cannot interleave with a
    # concurrent one. A no-op on SQLite, which backs only single-writer dev and
    # the tests - so the lock's effect is proven against Postgres
    # (test_postgres_integration.py), never inferred from SQLite.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(key)))


def _serialise_definition_writes(session: Session) -> None:
    _advisory_xact_lock(session, _DEFINITIONS_LOCK_KEY)


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
    # Strict: backs the publish gate, where every rule must hold. Booking reads
    # go through active_definition (lenient); golden replay validates a
    # book-only view.
    return CarrierDefinition.model_validate(row.data)


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
    # The booking and manifest-send runtime path: a published row loads
    # leniently so a since-tightened rule cannot strand a live carrier.
    row = active_definition_row(session, carrier)
    return CarrierDefinition.load(row.data) if row is not None else None


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


def missing_config_keys(
    definition: CarrierDefinition, config_data: dict[str, object]
) -> list[str]:
    """The config.* keys a definition references that config lacks, resolved by
    path like the render engine reads it. Sorted for a stable message;
    history-independent unlike the render gate."""
    missing: list[str] = []
    for path in definition.referenced_config_keys():
        node: object = config_data
        resolved = True
        for segment in path.split("."):
            if isinstance(node, dict) and segment in node:
                node = node[segment]
            elif (
                isinstance(node, list)
                and segment.isdigit()
                and int(segment) < len(node)
            ):
                node = node[int(segment)]
            else:
                resolved = False
                break
        # A null value is present but renders as the literal "None", so it is
        # not a provided key - treat it as absent, like a missing one.
        if not resolved or node is None:
            missing.append(path)
    return sorted(missing)


def upsert_carrier_config(
    session: Session, carrier: str, data: dict[str, object]
) -> None:
    # Serialised against the merge below: a PUT racing a PATCH must not let the
    # merge write back a snapshot that predates the replace.
    _advisory_xact_lock(session, _CONFIG_LOCK_KEY)
    row = session.get(CarrierConfig, carrier)
    if row is None:
        session.add(CarrierConfig(carrier=carrier, data=data))
    else:
        row.data = data
    session.flush()


def merge_carrier_config(
    session: Session, carrier: str, patch: dict[str, object]
) -> dict[str, object]:
    """Shallow-merges patch into the stored config: a patch value wins, an
    omitted top-level key survives. Nested values are replaced wholesale, not
    deep-merged."""
    # A read-modify-write, so lock first: two concurrent merges would otherwise
    # each read the same row and the last commit would silently lose the other's
    # key - the clobber this endpoint exists to prevent.
    _advisory_xact_lock(session, _CONFIG_LOCK_KEY)
    row = session.get(CarrierConfig, carrier)
    merged = {**row.data, **patch} if row is not None else dict(patch)
    if row is None:
        session.add(CarrierConfig(carrier=carrier, data=merged))
    else:
        row.data = merged
    session.flush()
    return merged
