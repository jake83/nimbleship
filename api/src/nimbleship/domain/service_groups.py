"""The Service Group catalogue (CONTEXT.md, ADR 0012).

Service groups are data, never constants (CLAUDE.md): the catalogue rows are
what rulebook declarations may reference, and draft validation checks against
them at authoring time. Codes are referenced by immutable rulebook versions, so
the catalogue supports create and update but not delete - removing a code would
orphan every stored version that names it. Codes are adopted verbatim from the
WMS (no remap), so the Legacy Interface can validate an accepted group code
against this catalogue directly."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nimbleship.models import ServiceGroup

# Demo seed for fresh installs, mirroring the demo rulebook: the demo Drop Out
# services declare membership of ECONOMY, so the legacy path allocates end to
# end. Real installs manage the catalogue via the API - never in code.
_DEMO_SERVICE_GROUPS: list[dict[str, str]] = [
    {
        "code": "ECONOMY",
        "name": "Economy",
        "description": "Economy carrier services.",
    },
    {
        "code": "NEXTDAY",
        "name": "Next Day",
        "description": "Guaranteed next-day carrier services.",
    },
]

# App-wide advisory lock key for catalogue seeding; distinct from every other
# write concern (see db.py).
_SERVICE_GROUPS_LOCK_KEY = 815_009


def _seed_if_fresh(session: Session) -> None:
    """Seed the demo catalogue when NO row exists, following the rulebook seed:
    double-checked under a Postgres advisory lock so the hot path only locks in
    the once-per-install case. On SQLite the lock is a no-op and a first-ever
    race would surface as a primary-key conflict rather than a double seed -
    accepted as benign."""
    exists = session.execute(select(ServiceGroup.code).limit(1)).scalar_one_or_none()
    if exists is not None:
        return
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(_SERVICE_GROUPS_LOCK_KEY)))
        exists = session.execute(
            select(ServiceGroup.code).limit(1)
        ).scalar_one_or_none()
        if exists is not None:
            return
    session.add_all(ServiceGroup(**group) for group in _DEMO_SERVICE_GROUPS)
    session.flush()


def known_service_group_codes(session: Session) -> set[str]:
    """Every code in the catalogue - what rulebook drafts may reference and what
    the Legacy Interface validates accepted codes against."""
    _seed_if_fresh(session)
    return set(session.execute(select(ServiceGroup.code)).scalars())


def list_service_groups(session: Session) -> list[ServiceGroup]:
    _seed_if_fresh(session)
    return list(
        session.execute(select(ServiceGroup).order_by(ServiceGroup.code)).scalars()
    )


def get_service_group(session: Session, code: str) -> ServiceGroup | None:
    _seed_if_fresh(session)
    return session.get(ServiceGroup, code)


def _code_taken(session: Session, code: str) -> bool:
    return session.get(ServiceGroup, code) is not None


def create_service_group(
    session: Session, code: str, name: str, description: str
) -> ServiceGroup | None:
    """Add a service group; None means the code is already taken. Behind this
    pre-check the primary key is the last line of defence against a duplicate
    race - callers must treat IntegrityError as the same conflict."""
    _seed_if_fresh(session)
    if _code_taken(session, code):
        return None
    row = ServiceGroup(code=code, name=name, description=description)
    session.add(row)
    session.flush()
    return row
