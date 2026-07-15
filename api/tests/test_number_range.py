"""Number ranges: some carriers make the client mint each consignment's
identifier from a sequential range. The counter is durable and must never
hand two callers the same number; because the render path is pure,
allocation happens BEFORE render and reaches the plugin as an injected
`allocated` fact."""

import logging
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker

from nimbleship.db import Base
from nimbleship.engine.field_plugins import field_plugin
from nimbleship.engine.plugins.number_range import (
    RangeExhausted,
    _gs1_check_digit,
    allocate_number,
    assemble_sscc,
    sscc_sequence_name,
    sscc_serial_width,
)
from nimbleship.models import CarrierNumberSequence


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db_session:
        yield db_session
    engine.dispose()


def test_a_fresh_sequence_starts_at_one_and_increments(session: Session) -> None:
    numbers = [
        allocate_number(session, "palletforce", "consignment_number") for _ in range(3)
    ]

    assert numbers == ["1", "2", "3"]


def test_sequences_are_independent_per_carrier_and_name(session: Session) -> None:
    assert allocate_number(session, "palletforce", "consignment_number") == "1"
    assert allocate_number(session, "palletforce", "consignment_number") == "2"

    assert allocate_number(session, "other", "consignment_number") == "1"
    assert allocate_number(session, "palletforce", "other_range") == "1"


def test_the_range_wraps_to_one_after_its_last_value(session: Session) -> None:
    session.add(
        CarrierNumberSequence(
            carrier="palletforce", name="consignment_number", next_value=9_999_999
        )
    )
    session.flush()

    assert allocate_number(session, "palletforce", "consignment_number") == "9999999"
    assert allocate_number(session, "palletforce", "consignment_number") == "1"


def test_the_halt_policy_claims_up_to_the_limit_then_raises(session: Session) -> None:
    # A halt range never wraps: reissuing a live SSCC would be unsafe. The
    # limit value is still claimable; the next allocation is refused loudly.
    numbers = [
        allocate_number(session, "dachser", "sscc", wrap_after=3, policy="halt")
        for _ in range(3)
    ]
    assert numbers == ["1", "2", "3"]

    with pytest.raises(RangeExhausted, match="sscc"):
        allocate_number(session, "dachser", "sscc", wrap_after=3, policy="halt")


def test_a_halt_range_with_no_capacity_is_exhausted_before_the_first_claim(
    session: Session,
) -> None:
    # wrap_after below 1 leaves nothing claimable; a fresh range must not still
    # hand out "1" past its own limit.
    with pytest.raises(RangeExhausted, match="no capacity"):
        allocate_number(session, "dachser", "empty", wrap_after=0, policy="halt")


def test_wrap_is_the_default_policy(session: Session) -> None:
    session.add(
        CarrierNumberSequence(
            carrier="palletforce", name="c", next_value=3, policy="wrap"
        )
    )
    session.flush()
    # No policy argument still wraps, so existing callers are unchanged.
    assert allocate_number(session, "palletforce", "c", wrap_after=3) == "3"
    assert allocate_number(session, "palletforce", "c", wrap_after=3) == "1"


def test_a_ranges_policy_is_stored_and_a_later_switch_is_refused(
    session: Session,
) -> None:
    allocate_number(session, "dachser", "r", wrap_after=3, policy="halt")
    # The policy is fixed on the row at creation.
    row = session.get(CarrierNumberSequence, ("dachser", "r"))
    assert row is not None and row.policy == "halt"

    # A later call with a different policy is refused, so an exhausted halt
    # range can never be cycled - and thus reissued - by a stray wrap call.
    with pytest.raises(ValueError, match="created with policy 'halt'"):
        allocate_number(session, "dachser", "r", wrap_after=3, policy="wrap")


def test_a_legacy_row_without_a_policy_backfills_on_allocation(
    session: Session,
) -> None:
    # A row created before the policy column has policy None; it keeps working
    # and adopts its policy on the next allocation.
    session.add(CarrierNumberSequence(carrier="x", name="y", next_value=5))
    session.flush()

    assert allocate_number(session, "x", "y", wrap_after=100, policy="wrap") == "5"
    row = session.get(CarrierNumberSequence, ("x", "y"))
    assert row is not None and row.policy == "wrap"


def test_a_range_cannot_shrink_its_wrap_after_but_can_widen_it(
    session: Session,
) -> None:
    allocate_number(session, "palletforce", "c", wrap_after=100, policy="wrap")
    # The bound is stored on the row at creation.
    row = session.get(CarrierNumberSequence, ("palletforce", "c"))
    assert row is not None and row.wrap_after == 100

    # Shrinking is refused: numbers already issued beyond the smaller bound
    # would be wrapped early and reissued.
    with pytest.raises(ValueError, match="cannot shrink"):
        allocate_number(session, "palletforce", "c", wrap_after=50, policy="wrap")

    # Widening is safe (the live counter is still under the larger ceiling) and
    # adopts the new bound.
    assert allocate_number(session, "palletforce", "c", wrap_after=200) == "2"
    row = session.get(CarrierNumberSequence, ("palletforce", "c"))
    assert row is not None and row.wrap_after == 200


def test_a_halt_range_cannot_change_its_wrap_after_at_all(session: Session) -> None:
    # A halt range's capacity is fixed at creation - unlike a wrap range it may
    # not even widen.
    allocate_number(session, "dachser", "sscc", wrap_after=3, policy="halt")
    row = session.get(CarrierNumberSequence, ("dachser", "sscc"))
    assert row is not None and row.wrap_after == 3

    with pytest.raises(ValueError, match="fixed capacity"):
        allocate_number(session, "dachser", "sscc", wrap_after=5, policy="halt")
    with pytest.raises(ValueError, match="fixed capacity"):
        allocate_number(session, "dachser", "sscc", wrap_after=2, policy="halt")

    # The stored bound is untouched by the refused calls, so the range still
    # halts exactly at its original 3 - never revived past it.
    assert [
        allocate_number(session, "dachser", "sscc", wrap_after=3, policy="halt")
        for _ in range(2)
    ] == ["2", "3"]
    with pytest.raises(RangeExhausted):
        allocate_number(session, "dachser", "sscc", wrap_after=3, policy="halt")


def test_widening_an_exhausted_halt_range_does_not_revive_it(
    session: Session,
) -> None:
    # The scenario that matters most: bumping the bound must not revive an
    # exhausted halt range - those numbers are live codes.
    for _ in range(2):
        allocate_number(session, "dachser", "sscc", wrap_after=2, policy="halt")
    with pytest.raises(RangeExhausted):
        allocate_number(session, "dachser", "sscc", wrap_after=2, policy="halt")

    with pytest.raises(ValueError, match="fixed capacity"):
        allocate_number(session, "dachser", "sscc", wrap_after=1000, policy="halt")

    with pytest.raises(RangeExhausted):
        allocate_number(session, "dachser", "sscc", wrap_after=2, policy="halt")


def test_a_halt_row_with_no_recorded_bound_is_refused(session: Session) -> None:
    # A legacy halt row predating the wrap_after column has no recorded
    # capacity. Its bound never backfills on the exhausted path (the raise
    # precedes the write), so a later larger wrap_after would revive it. Refuse
    # any allocation instead - a halt range needs a fixed bound.
    session.add(
        CarrierNumberSequence(
            carrier="dachser", name="sscc", next_value=10, policy="halt"
        )
    )
    session.flush()

    with pytest.raises(RangeExhausted, match="no recorded capacity"):
        allocate_number(session, "dachser", "sscc", wrap_after=3, policy="halt")
    # The exact revival the guard closes: a larger bound must not resurrect it.
    with pytest.raises(RangeExhausted, match="no recorded capacity"):
        allocate_number(session, "dachser", "sscc", wrap_after=1000, policy="halt")


def test_a_wrap_range_never_issues_an_out_of_range_legacy_counter(
    session: Session,
) -> None:
    # A legacy row (null wrap_after) whose counter sits beyond a now-reduced
    # bound must wrap to 1, never hand out the out-of-range value.
    session.add(CarrierNumberSequence(carrier="x", name="y", next_value=5000))
    session.flush()

    assert allocate_number(session, "x", "y", wrap_after=999, policy="wrap") == "1"
    row = session.get(CarrierNumberSequence, ("x", "y"))
    assert row is not None and row.next_value == 2 and row.wrap_after == 999


def test_concurrent_allocations_never_hand_out_the_same_number(
    tmp_path: Path,
) -> None:
    """A barrier holds both sessions just before their guarded UPDATEs, so
    both have read the same next_value; the UPDATE must refuse the loser
    rather than hand both callers the same number."""
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    with factory() as setup:
        allocate_number(setup, "palletforce", "consignment_number")
        setup.commit()

    barrier = threading.Barrier(2, timeout=10)
    outcomes: dict[str, str] = {}

    def hold_updates_at_the_barrier(state: ORMExecuteState) -> None:
        if state.is_update:
            barrier.wait()

    def attempt(key: str) -> None:
        db_session = factory()
        event.listen(db_session, "do_orm_execute", hold_updates_at_the_barrier)
        try:
            number = allocate_number(db_session, "palletforce", "consignment_number")
            db_session.commit()
            outcomes[key] = number
        except ValueError:
            db_session.rollback()
            outcomes[key] = "conflict"
        finally:
            db_session.close()

    threads = [threading.Thread(target=attempt, args=(key,)) for key in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    engine.dispose()

    assert sorted(outcomes.values()) == ["2", "conflict"]


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def range_logs() -> Iterator[_RecordingHandler]:
    # Attach directly to the allocator's logger rather than via caplog, whose
    # root-propagation capture is fragile here. `disabled` is forced off because
    # running Alembic in-process (the Postgres integration tests) calls
    # fileConfig with disable_existing_loggers=True, which disables this logger
    # for the rest of the test process - a test-only artifact (migrations run
    # apart from the API in production).
    handler = _RecordingHandler()
    logger = logging.getLogger("nimbleship.engine.plugins.number_range")
    previous_level, previous_disabled = logger.level, logger.disabled
    logger.setLevel(logging.WARNING)
    logger.disabled = False
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.disabled = previous_disabled


def test_a_low_range_emits_a_structured_warning_with_a_remaining_count(
    session: Session, range_logs: _RecordingHandler
) -> None:
    session.add(
        CarrierNumberSequence(
            carrier="dachser",
            name="sscc",
            next_value=98,
            policy="halt",
            wrap_after=100,
        )
    )
    session.flush()

    allocate_number(
        session, "dachser", "sscc", wrap_after=100, policy="halt", reorder_threshold=5
    )

    [record] = [r for r in range_logs.records if "running low" in r.getMessage()]
    # 100 last claimable, 98 just claimed -> 2 left; queryable off the record.
    assert record.remaining == 2  # type: ignore[attr-defined]
    assert record.carrier == "dachser"  # type: ignore[attr-defined]


def test_a_healthy_range_emits_no_warning(
    session: Session, range_logs: _RecordingHandler
) -> None:
    allocate_number(
        session, "dachser", "sscc", wrap_after=100, policy="halt", reorder_threshold=5
    )

    assert not [r for r in range_logs.records if "running low" in r.getMessage()]


def test_gs1_check_digit_matches_the_standard_algorithm() -> None:
    # GS1's own worked example (GTIN-12 body 03600029145 -> check digit 2),
    # which pins the mod-10 weighting; plus two hand-checked 17-digit bodies.
    assert _gs1_check_digit("03600029145") == "2"
    assert _gs1_check_digit("0" * 17) == "0"
    assert _gs1_check_digit("12345678901234567") == "5"


def test_assemble_sscc_builds_prefix_suffix_and_check_digit() -> None:
    # A fixed 18-digit reference: an 11-digit prefix and serial 42 give the
    # 17-digit body 01234567890000042, whose GS1 check digit is 5. Asserting
    # the literal (not the function's own check-digit output) makes a wrong
    # check digit fail here, not only in the algorithm test.
    sscc = assemble_sscc("01234567890", 42)
    assert sscc == "012345678900000425"
    assert len(sscc) == 18


def test_assemble_sscc_and_width_reject_bad_prefixes_and_serials() -> None:
    # A non-digit or non-ASCII prefix (superscript one passes str.isdigit but
    # int() rejects it) is refused before the check-digit sum.
    with pytest.raises(ValueError, match="not a digit string"):
        sscc_serial_width("12x")
    with pytest.raises(ValueError, match="not a digit string"):
        sscc_serial_width("\u00b9234567890")
    # A prefix that fills all 17 body digits leaves no room for a serial.
    with pytest.raises(ValueError, match="no room"):
        sscc_serial_width("0" * 17)
    # A serial wider than the room the prefix leaves, or not positive, cannot
    # fit.
    with pytest.raises(ValueError, match="does not fit"):
        assemble_sscc("01234567890", 1234567)
    with pytest.raises(ValueError, match="does not fit"):
        assemble_sscc("01234567890", 0)


def test_sscc_sequences_key_on_the_prefix_so_a_new_range_starts_fresh(
    session: Session,
) -> None:
    old = sscc_sequence_name("9610000")
    new = sscc_sequence_name("9620000")
    assert old != new

    assert (
        allocate_number(session, "dachser", old, wrap_after=999, policy="halt") == "1"
    )
    assert (
        allocate_number(session, "dachser", old, wrap_after=999, policy="halt") == "2"
    )
    # Provisioning a new range (a config prefix change) is a fresh sequence at
    # 1; the spent prefix's counter stays frozen as an audit of what issued.
    assert (
        allocate_number(session, "dachser", new, wrap_after=999, policy="halt") == "1"
    )


def test_the_consignment_number_plugin_formats_the_allocated_fact() -> None:
    plugin = field_plugin("palletforce_consignment_number")

    computed = plugin.compute({"allocated": {"consignment_number": "42"}})

    assert computed == "0000042"


def test_the_plugin_fails_loudly_when_no_number_was_allocated() -> None:
    plugin = field_plugin("palletforce_consignment_number")

    with pytest.raises(ValueError, match="allocate_number"):
        plugin.compute({"shipment": {"order_number": "95000254580"}})


def test_the_plugin_refuses_numbers_wider_than_the_range() -> None:
    plugin = field_plugin("palletforce_consignment_number")

    with pytest.raises(ValueError, match="7"):
        plugin.compute({"allocated": {"consignment_number": "12345678"}})


def test_the_plugin_refuses_a_non_numeric_allocation() -> None:
    plugin = field_plugin("palletforce_consignment_number")

    # "\u00b2" is a superscript two: str.isdigit accepts it but int() rejects
    # it, so the shared validator refuses it up front like any other non-number.
    for garbage in ("UMB42", "", True, None, "\u00b2"):
        with pytest.raises(ValueError, match="not a number"):
            plugin.compute({"allocated": {"consignment_number": garbage}})
