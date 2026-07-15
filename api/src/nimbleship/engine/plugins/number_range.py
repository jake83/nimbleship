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


def _is_ascii_digits(value: str) -> bool:
    # str.isdigit accepts non-ASCII "digits" (superscripts, etc.) that int()
    # rejects; a range number may only contain ASCII 0-9.
    return value.isascii() and value.isdigit()


def _warn_if_low(
    carrier: str,
    name: str,
    claimed: int,
    wrap_after: int,
    reorder_threshold: int | None,
) -> None:
    """Emit a structured 'running low' warning once a range's remaining count
    reaches the reorder threshold. The remaining count rides the log record so
    it is queryable; delivering the alert (email/Teams) is Phase 7 - the
    allocation path must not depend on a notification channel."""
    if reorder_threshold is None:
        return
    remaining = wrap_after - claimed
    if remaining <= reorder_threshold:
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
    reorder_threshold: int | None = None,
) -> str:
    """Claim the next value of a carrier's number range, as a plain
    decimal string (formatting to width is the field plugin's job).

    `wrap_after` is the last claimable value; `policy` decides what happens
    beyond it - `wrap` cycles to 1, `halt` raises RangeExhausted. When
    `reorder_threshold` is set, a structured warning is logged once the
    remaining count reaches it.

    `policy` and `wrap_after` are fixed on the row at creation: a later policy
    switch, or a shrinking `wrap_after`, is refused - an exhausted halt range
    must never be reissued, nor a live range wrapped early. Widening is allowed.

    Same hardening shape as the definition rails' publish: Postgres
    serialises allocators on an advisory lock, and the guarded UPDATE is
    the engine-agnostic backstop - a lost race raises rather than ever
    handing out a duplicate."""
    _serialise_allocations(session)
    existing = session.execute(
        select(
            CarrierNumberSequence.next_value,
            CarrierNumberSequence.policy,
            CarrierNumberSequence.wrap_after,
        ).where(
            CarrierNumberSequence.carrier == carrier,
            CarrierNumberSequence.name == name,
        )
    ).one_or_none()
    if existing is None:
        # A halt range with no capacity (wrap_after below 1) can claim nothing,
        # so it is exhausted before the first allocation.
        if policy == "halt" and wrap_after < 1:
            raise RangeExhausted(
                f"number range '{name}' for carrier '{carrier}' has no capacity "
                f"(wrap_after={wrap_after})"
            )
        # A fresh range: claiming value 1 and creating the counter are one
        # insert, so a concurrent first caller trips the primary key. The
        # policy and wrap_after are stored so neither can later be switched.
        session.add(
            CarrierNumberSequence(
                carrier=carrier,
                name=name,
                next_value=2,
                policy=policy,
                wrap_after=wrap_after,
            )
        )
        session.flush()
        _warn_if_low(carrier, name, 1, wrap_after, reorder_threshold)
        return "1"
    current, stored_policy, stored_wrap_after = existing
    # A range's policy is fixed at creation: reusing its name with a different
    # policy is refused, so an exhausted halt range can never be cycled by a
    # stray wrap call. A row created before the policy column is null and
    # backfills on this allocation.
    if stored_policy is not None and stored_policy != policy:
        raise ValueError(
            f"number range '{name}' for carrier '{carrier}' was created with "
            f"policy '{stored_policy}', not '{policy}'"
        )
    # Shrinking is refused: numbers issued beyond the smaller ceiling would
    # wrap early and reissue. Widening is safe and adopts the new bound.
    if stored_wrap_after is not None and stored_wrap_after > wrap_after:
        raise ValueError(
            f"number range '{name}' for carrier '{carrier}' cannot shrink from "
            f"wrap_after {stored_wrap_after} to {wrap_after}: numbers already "
            "issued beyond the new bound would be reissued"
        )
    if policy == "halt":
        # `current` is the value about to be claimed and `wrap_after` the last
        # claimable one, so the counter only passes it after the last value was
        # handed out: the range is spent and must not reissue.
        if current > wrap_after:
            raise RangeExhausted(
                f"number range '{name}' for carrier '{carrier}' is exhausted "
                f"at {wrap_after}: provision a new range"
            )
        claimed_value = current
        following = current + 1
    elif current > wrap_after:
        # A counter already past the bound (a legacy null-bound row, or a fresh
        # small range) wraps to 1 rather than issue an out-of-range number.
        claimed_value = 1
        following = 2
    else:
        claimed_value = current
        following = 1 if current >= wrap_after else current + 1
    claimed: CursorResult[object] = session.execute(  # type: ignore[assignment]
        update(CarrierNumberSequence)
        .where(
            CarrierNumberSequence.carrier == carrier,
            CarrierNumberSequence.name == name,
            CarrierNumberSequence.next_value == current,
        )
        .values(next_value=following, policy=policy, wrap_after=wrap_after)
    )
    if claimed.rowcount != 1:
        raise ValueError(
            f"number range '{name}' for carrier '{carrier}' was claimed by "
            "a concurrent request"
        )
    _warn_if_low(carrier, name, claimed_value, wrap_after, reorder_threshold)
    return str(claimed_value)


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


def sscc_serial_width(prefix: str) -> int:
    """How many serial digits a prefix leaves in the 17-digit body: the
    allocator's wrap_after for the range is 10**width - 1 (the last serial)."""
    if not _is_ascii_digits(prefix):
        raise ValueError(f"SSCC prefix is not a digit string: {prefix!r}")
    width = SSCC_BODY_DIGITS - len(prefix)
    if width <= 0:
        raise ValueError(
            f"SSCC prefix {prefix!r} leaves no room for a serial in "
            f"{SSCC_BODY_DIGITS} body digits"
        )
    return width


def sscc_wrap_after(prefix: str) -> int:
    """The allocator's `wrap_after` for an SSCC range: the widest serial the
    digits the prefix leaves can hold, kept beside the width and assembly rules
    so the body-digit definition lives in one place."""
    return int(10 ** sscc_serial_width(prefix)) - 1


def assemble_sscc(prefix: str, serial: int) -> str:
    """Assemble an 18-digit SSCC: the carrier prefix, the serial zero-padded to
    fill the 17-digit body, and the GS1 mod-10 check digit. Shared by the
    booking dispatch (which mints one per parcel) and the SSCCField plugin, so
    render-time and mint-time SSCCs are assembled the one way."""
    width = sscc_serial_width(prefix)
    if serial <= 0 or len(str(serial)) > width:
        raise ValueError(f"SSCC serial {serial} does not fit {width} digits")
    body = prefix + f"{serial:0{width}d}"
    return body + _gs1_check_digit(body)


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
        isinstance(raw, str) and _is_ascii_digits(raw)
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
        # A non-ASCII "digit" would pass str.isdigit but blow up later inside
        # the check-digit sum's int(); _is_ascii_digits refuses it up front,
        # the same guard _allocated_number applies to the serial.
        if not isinstance(prefix, str) or not _is_ascii_digits(prefix):
            raise ValueError(
                f"config '{self._prefix_key}' is not a digit-string SSCC prefix: "
                f"{prefix!r}"
            )
        return assemble_sscc(prefix, _allocated_number(facts, self._suffix_fact))


register(
    "palletforce_consignment_number",
    AllocatedNumberField(fact="consignment_number", width=7),
)
register("sscc", SSCCField(prefix_key="sscc_prefix", suffix_fact="sscc_suffix"))
