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

import logging
from typing import Literal

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from nimbleship.engine.field_plugins import register
from nimbleship.models import CarrierNumberSequence

logger = logging.getLogger(__name__)

# Registered in the advisory-lock key list in nimbleship/db.py.
_NUMBER_SEQUENCES_LOCK_KEY = 815_006

# Ranges back fixed-width identifiers; when the last value is claimed the
# counter wraps to 1, matching how carriers cycle their ranges. The
# default suits 7-digit ranges.
DEFAULT_WRAP_AFTER = 9_999_999

# How a range behaves once its last value is claimed. `wrap` cycles back to 1;
# `halt` refuses further allocation, for ranges where reissuing a live number
# is unsafe (an SSCC identifies a physical unit still in the network).
ExhaustionPolicy = Literal["wrap", "halt"]


class RangeExhausted(Exception):
    """A `halt` range has no values left: its last number is already claimed
    and wrapping would reissue a live code. The carrier must provision a new
    range (a config prefix change), which keys a fresh sequence."""


def _serialise_allocations(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(select(func.pg_advisory_xact_lock(_NUMBER_SEQUENCES_LOCK_KEY)))


def _warn_if_low(
    carrier: str, name: str, claimed: int, wrap_after: int, low_water: int | None
) -> None:
    """Emit a structured 'running low' warning once a range's remaining count
    reaches the soft threshold. The remaining count rides the log record so it
    is queryable; delivering the alert (email/Teams) is Phase 7 - the
    allocation path must not depend on a notification channel."""
    if low_water is None:
        return
    remaining = wrap_after - claimed
    if remaining <= low_water:
        logger.warning(
            "number range running low",
            extra={
                "carrier": carrier,
                "range": name,
                "remaining": remaining,
                "wrap_after": wrap_after,
            },
        )


def allocate_number(
    session: Session,
    carrier: str,
    name: str,
    *,
    wrap_after: int = DEFAULT_WRAP_AFTER,
    policy: ExhaustionPolicy = "wrap",
    low_water: int | None = None,
) -> str:
    """Claim the next value of a carrier's number range, as a plain
    decimal string (formatting to width is the field plugin's job).

    `wrap_after` is the last claimable value; `policy` decides what happens
    beyond it - `wrap` cycles to 1, `halt` raises RangeExhausted. When
    `low_water` is set, a structured warning is logged once the remaining
    count reaches it.

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
        _warn_if_low(carrier, name, 1, wrap_after, low_water)
        return "1"
    if policy == "halt":
        if current > wrap_after:
            raise RangeExhausted(
                f"number range '{name}' for carrier '{carrier}' is exhausted "
                f"at {wrap_after}: provision a new range"
            )
        following = current + 1
    else:
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
    _warn_if_low(carrier, name, current, wrap_after, low_water)
    return str(current)


# An SSCC is 18 digits: a 17-digit body (carrier prefix + serial) and one
# GS1 mod-10 check digit.
SSCC_BODY_DIGITS = 17


def _gs1_check_digit(body: str) -> str:
    """The GS1 mod-10 check digit for a numeric body: weighting from the
    rightmost body digit alternates 3, 1, 3, 1...; the check digit is whatever
    brings the weighted total to a multiple of ten."""
    total = sum(
        int(digit) * (3 if position % 2 == 0 else 1)
        for position, digit in enumerate(reversed(body))
    )
    return str((10 - total % 10) % 10)


def sscc_sequence_name(prefix: str) -> str:
    """The number-sequence name for an SSCC range, keyed on the prefix so a
    range refresh is just a config prefix change: allocation sees a fresh
    sequence starting at 1, and the spent prefix's counter is frozen in the
    table as an audit of what was issued."""
    return f"sscc:{prefix}"


def _allocated_number(facts: dict[str, object], fact: str) -> int:
    """Read and validate a pre-allocated range number from facts["allocated"]
    - injected by the dispatch integration after allocate_number - failing
    loudly when allocation has not run or the value is not a positive
    number."""
    allocated = facts.get("allocated")
    if not isinstance(allocated, dict) or fact not in allocated:
        raise ValueError(
            f"no allocated '{fact}' fact: allocate_number must run before "
            "render and inject its result"
        )
    raw = allocated[fact]
    valid = (isinstance(raw, int) and not isinstance(raw, bool)) or (
        isinstance(raw, str) and raw.isdigit()
    )
    if not valid:
        raise ValueError(f"allocated '{fact}' is not a number: {raw!r}")
    number = int(raw)
    if number <= 0:
        raise ValueError(f"allocated '{fact}' must be positive, not {number}")
    return number


def _zero_pad(number: int, fact: str, width: int) -> str:
    if len(str(number)) > width:
        raise ValueError(f"allocated '{fact}' {number} does not fit {width} digits")
    return f"{number:0{width}d}"


class AllocatedNumberField:
    """A computed field that formats a pre-allocated range number to fixed
    width."""

    def __init__(self, fact: str, width: int) -> None:
        self._fact = fact
        self._width = width

    def compute(self, facts: dict[str, object]) -> object:
        return _zero_pad(_allocated_number(facts, self._fact), self._fact, self._width)


class SSCCField:
    """Assembles an 18-digit SSCC from a carrier-provisioned prefix (config)
    and an allocated serial (facts["allocated"]): the prefix and zero-padded
    serial fill the 17-digit body, then the GS1 mod-10 check digit closes it.
    The serial width is whatever the prefix leaves, so a longer prefix simply
    means a shorter serial."""

    def __init__(self, prefix_key: str, suffix_fact: str) -> None:
        self._prefix_key = prefix_key
        self._suffix_fact = suffix_fact

    def compute(self, facts: dict[str, object]) -> object:
        config = facts.get("config")
        prefix = config.get(self._prefix_key) if isinstance(config, dict) else None
        if not isinstance(prefix, str) or not prefix.isdigit():
            raise ValueError(
                f"config '{self._prefix_key}' is not a digit-string SSCC prefix: "
                f"{prefix!r}"
            )
        width = SSCC_BODY_DIGITS - len(prefix)
        if width <= 0:
            raise ValueError(
                f"config '{self._prefix_key}' SSCC prefix {prefix!r} leaves no room "
                f"for a serial in {SSCC_BODY_DIGITS} body digits"
            )
        serial = _zero_pad(
            _allocated_number(facts, self._suffix_fact), self._suffix_fact, width
        )
        body = prefix + serial
        return body + _gs1_check_digit(body)


register(
    "palletforce_consignment_number",
    AllocatedNumberField(fact="consignment_number", width=7),
)
register("sscc", SSCCField(prefix_key="sscc_prefix", suffix_fact="sscc_suffix"))
