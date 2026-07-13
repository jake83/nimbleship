"""Number ranges and the computed fields they back.

Some carriers require the client to mint each consignment's identifier
from a fixed-width sequential range. The carrier_number_sequences table
holds one durable counter per (carrier, name); allocate_number claims the
next value and must never hand two callers the same number.

Allocation is stateful and the render path is pure (see
nimbleship.engine.field_plugins), so the two halves meet through a fact:
the dispatch integration calls allocate_number BEFORE rendering and
injects the result as facts["allocated"][<name>]; the registered
computed-field plugin then formats that fact into the mapped field. A
definition entry naming "palletforce_consignment_number" therefore only
renders once an allocated consignment_number fact is present - a missing
allocation fails loudly, never silently mints on the render path.
"""

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from nimbleship.engine.field_plugins import register
from nimbleship.models import CarrierNumberSequence

# Registered in the advisory-lock key list in nimbleship/db.py.
_NUMBER_SEQUENCES_LOCK_KEY = 815_006

# Ranges back fixed-width identifiers; when the last value is claimed the
# counter wraps to 1, matching how carriers cycle their ranges. The
# default suits 7-digit ranges.
DEFAULT_WRAP_AFTER = 9_999_999


def _serialise_allocations(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(_NUMBER_SEQUENCES_LOCK_KEY)))


def allocate_number(
    session: Session,
    carrier: str,
    name: str,
    *,
    wrap_after: int = DEFAULT_WRAP_AFTER,
) -> str:
    """Claim the next value of a carrier's number range, as a plain
    decimal string (formatting to width is the field plugin's job).

    Same hardening shape as the definition rails' publish: Postgres
    serialises allocators on an advisory lock, and the guarded UPDATE is
    the engine-agnostic backstop - a lost race raises rather than ever
    handing out a duplicate."""
    _serialise_allocations(session)
    current = session.execute(
        select(CarrierNumberSequence.next_value).where(
            CarrierNumberSequence.carrier == carrier,
            CarrierNumberSequence.name == name,
        )
    ).scalar_one_or_none()
    if current is None:
        # A fresh range: claiming value 1 and creating the counter are one
        # insert, so a concurrent first caller trips the primary key.
        session.add(CarrierNumberSequence(carrier=carrier, name=name, next_value=2))
        session.flush()
        return "1"
    following = 1 if current >= wrap_after else current + 1
    claimed: CursorResult[object] = session.execute(  # type: ignore[assignment]
        update(CarrierNumberSequence)
        .where(
            CarrierNumberSequence.carrier == carrier,
            CarrierNumberSequence.name == name,
            CarrierNumberSequence.next_value == current,
        )
        .values(next_value=following)
    )
    if claimed.rowcount != 1:
        raise ValueError(
            f"number range '{name}' for carrier '{carrier}' was claimed by "
            "a concurrent request"
        )
    return str(current)


class AllocatedNumberField:
    """A computed field that formats a pre-allocated range number to fixed
    width. Reads facts["allocated"][<fact>] - injected by the dispatch
    integration after allocate_number - and fails loudly when allocation
    has not run or the value cannot fit the width."""

    def __init__(self, fact: str, width: int) -> None:
        self._fact = fact
        self._width = width

    def compute(self, facts: dict[str, object]) -> object:
        allocated = facts.get("allocated")
        if not isinstance(allocated, dict) or self._fact not in allocated:
            raise ValueError(
                f"no allocated '{self._fact}' fact: allocate_number must run "
                "before render and inject its result"
            )
        raw = allocated[self._fact]
        valid = (isinstance(raw, int) and not isinstance(raw, bool)) or (
            isinstance(raw, str) and raw.isdigit()
        )
        if not valid:
            raise ValueError(f"allocated '{self._fact}' is not a number: {raw!r}")
        number = int(raw)
        if number <= 0 or len(str(number)) > self._width:
            raise ValueError(
                f"allocated '{self._fact}' {number} does not fit {self._width} digits"
            )
        return f"{number:0{self._width}d}"


register(
    "palletforce_consignment_number",
    AllocatedNumberField(fact="consignment_number", width=7),
)
