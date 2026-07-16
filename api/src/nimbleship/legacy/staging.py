"""Staging store for the Legacy Interface's stateful lifecycle (ADR 0011):
create and allocate accumulate here, paperwork consumes it."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nimbleship.models import LegacyConsignmentStaging

# Registered in the advisory-lock list in nimbleship/db.py.
_STAGING_LOCK_KEY = 815_008


def _serialise_staging_writes(session: Session) -> None:
    # Serialises the read-modify-write below on Postgres, so two concurrent
    # creates for one order cannot both miss the pre-check and stage duplicate
    # rows. A no-op on SQLite, which backs only single-writer dev and the tests.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(_STAGING_LOCK_KEY)))


def _code_for(staging_id: int) -> str:
    # NimbleShip-native iterable handle, not a MetaPack DMC code (ADR 0011).
    return f"NS{staging_id:07d}"


def stage_created(session: Session, data: dict[str, object]) -> str:
    order_number = str(data["order_number"])
    _serialise_staging_writes(session)
    # A re-sent create for the same order reuses its row and code rather than
    # minting a second handle for one shipment.
    row = session.execute(
        select(LegacyConsignmentStaging).where(
            LegacyConsignmentStaging.order_number == order_number
        )
    ).scalar_one_or_none()
    if row is not None:
        row.created_data = data
        session.flush()
        assert row.consignment_code is not None  # minted when the row was created
        return row.consignment_code
    row = LegacyConsignmentStaging(order_number=order_number, created_data=data)
    session.add(row)
    session.flush()  # assigns the autoincrement id the code derives from
    code = _code_for(row.id)
    row.consignment_code = code
    session.flush()
    return code


def stage_allocation(
    session: Session, consignment_code: str, data: dict[str, object]
) -> bool:
    """Record the allocate call's intent on the staged row, returning whether
    the code was found. False means create has not run for it - the code only
    exists once create minted it (ADR 0011), so the caller faults."""
    # Same lock as stage_created: this read-modify-write shares the staging
    # table's one write concern, so an allocate cannot lose its write to a
    # concurrent create-resend rewriting the same row.
    _serialise_staging_writes(session)
    row = session.execute(
        select(LegacyConsignmentStaging).where(
            LegacyConsignmentStaging.consignment_code == consignment_code
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    row.allocation_data = data
    session.flush()
    return True
