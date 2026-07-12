"""True concurrent publishes: a barrier forces both sessions past their
reads before either flushes - the race the TestClient suite cannot
produce (refuter, PR #9, twice)."""

import threading
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from nimbleship.db import Base
from nimbleship.domain.allocation import ServiceDeclaration
from nimbleship.domain.rulebook import create_draft, publish
from nimbleship.models import RulebookVersion

SERVICES = [
    ServiceDeclaration(
        code="STD",
        carrier="dropout",
        name="Standard",
        weight_min_kg=Decimal("0"),
        weight_max_kg=Decimal("30"),
        countries=["GB"],
        cost=Decimal("4.50"),
        tie_break_order=1,
    )
]


def test_double_submitted_publish_conflicts_for_the_second_caller(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    with factory() as setup:
        draft = create_draft(setup, SERVICES, "test")
        version = draft.version
        setup.commit()

    barrier = threading.Barrier(2, timeout=10)
    outcomes: dict[str, str] = {}

    def attempt(key: str, session: Session) -> None:
        try:
            row = session.get(RulebookVersion, version)
            assert row is not None
            barrier.wait()
            publish(session, row)
            session.commit()
            outcomes[key] = "published"
        except ValueError:
            session.rollback()
            outcomes[key] = "conflict"
        finally:
            session.close()

    threads = [
        threading.Thread(target=attempt, args=(key, factory())) for key in ("a", "b")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    engine.dispose()

    assert sorted(outcomes.values()) == ["conflict", "published"]


def test_sequential_double_publish_conflicts_for_the_second_caller(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'race.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    with factory() as setup:
        draft = create_draft(setup, SERVICES, "test")
        version = draft.version
        setup.commit()

    session_a = factory()
    session_b = factory()
    try:
        row_a = session_a.get(RulebookVersion, version)
        row_b = session_b.get(RulebookVersion, version)
        assert row_a is not None and row_b is not None

        publish(session_a, row_a)
        session_a.commit()

        with pytest.raises(ValueError, match="draft"):
            publish(session_b, row_b)
    finally:
        session_a.close()
        session_b.close()
        engine.dispose()
