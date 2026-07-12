"""Number ranges: some carriers make the client mint each consignment's
identifier from a sequential range. The counter is durable and must never
hand two callers the same number; because the render path is pure,
allocation happens BEFORE render and reaches the plugin as an injected
`allocated` fact."""

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import ORMExecuteState, Session, sessionmaker

from nimbleship.db import Base
from nimbleship.engine.field_plugins import field_plugin
from nimbleship.engine.plugins.number_range import allocate_number
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

    for garbage in ("UMB42", "", True, None):
        with pytest.raises(ValueError, match="not a number"):
            plugin.compute({"allocated": {"consignment_number": garbage}})
