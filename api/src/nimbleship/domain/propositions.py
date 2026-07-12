"""The Delivery Proposition catalogue (CONTEXT.md).

Propositions are data, never constants (CLAUDE.md): the catalogue rows are
what rulebook declarations may reference, and draft validation checks
against them at authoring time. Codes are referenced by immutable rulebook
versions, so the catalogue supports create and update but not delete -
removing a code would orphan every stored version that names it."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nimbleship.models import DeliveryProposition

# Demo seed for fresh installs, mirroring the demo rulebook: enough to
# exercise the proposition filter end to end. Real installs manage the
# catalogue via the API - never in code.
_DEMO_PROPOSITIONS: list[dict[str, str]] = [
    {
        "code": "next-day",
        "name": "Next Day",
        "description": "Delivered the next working day.",
    },
    {
        "code": "economy",
        "name": "Economy",
        "description": "Delivered within the standard economy window.",
    },
]

# App-wide advisory lock key for catalogue seeding; distinct from the
# rulebook's key so the two seeds never serialise each other.
_PROPOSITIONS_LOCK_KEY = 815_004


def _seed_if_fresh(session: Session) -> None:
    """Seed the demo catalogue when NO row exists, following the rulebook
    seed: double-checked under a Postgres advisory lock so the hot path
    only locks in the once-per-install case. On SQLite the lock is a no-op
    and a first-ever race would surface as a primary-key conflict rather
    than a double seed - accepted as benign."""
    exists = session.execute(
        select(DeliveryProposition.code).limit(1)
    ).scalar_one_or_none()
    if exists is not None:
        return
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(_PROPOSITIONS_LOCK_KEY)))
        exists = session.execute(
            select(DeliveryProposition.code).limit(1)
        ).scalar_one_or_none()
        if exists is not None:
            return
    session.add_all(
        DeliveryProposition(**proposition) for proposition in _DEMO_PROPOSITIONS
    )
    session.flush()


def known_proposition_codes(session: Session) -> set[str]:
    """Every code in the catalogue - what rulebook drafts may reference."""
    _seed_if_fresh(session)
    return set(session.execute(select(DeliveryProposition.code)).scalars())


def list_propositions(session: Session) -> list[DeliveryProposition]:
    _seed_if_fresh(session)
    return list(
        session.execute(
            select(DeliveryProposition).order_by(DeliveryProposition.code)
        ).scalars()
    )


def get_proposition(session: Session, code: str) -> DeliveryProposition | None:
    _seed_if_fresh(session)
    return session.get(DeliveryProposition, code)


def _code_taken(session: Session, code: str) -> bool:
    return session.get(DeliveryProposition, code) is not None


def create_proposition(
    session: Session, code: str, name: str, description: str
) -> DeliveryProposition | None:
    """Add a proposition; None means the code is already taken. Behind this
    pre-check the primary key is the last line of defence against a
    duplicate race - callers must treat IntegrityError as the same
    conflict."""
    _seed_if_fresh(session)
    if _code_taken(session, code):
        return None
    row = DeliveryProposition(code=code, name=name, description=description)
    session.add(row)
    session.flush()
    return row
