"""Postgres-only integration tests: the advisory-lock branch and the
migrations, exercised against a real server. Skipped unless
NIMBLESHIP_TEST_POSTGRES_URL is set (CI provides a service container) -
closing the coverage gap the refuter flagged on PR #9."""

import os
import threading
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from nimbleship.db import Base
from nimbleship.domain.allocation import ServiceDeclaration
from nimbleship.domain.rulebook import active_rulebook, create_draft, publish
from nimbleship.models import RulebookVersion

POSTGRES_URL = os.environ.get("NIMBLESHIP_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    POSTGRES_URL is None,
    reason="needs NIMBLESHIP_TEST_POSTGRES_URL (Postgres service container)",
)

API_ROOT = Path(__file__).parent.parent

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


@pytest.fixture
def pg_engine() -> "Iterator[Engine]":
    assert POSTGRES_URL is not None
    engine = create_engine(POSTGRES_URL)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def test_alembic_upgrade_head_against_postgres() -> None:
    assert POSTGRES_URL is not None
    engine = create_engine(POSTGRES_URL)
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))

    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", POSTGRES_URL)
    command.upgrade(config, "head")

    assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()


def test_concurrent_publishes_of_same_draft_pick_exactly_one_winner(
    pg_engine: Engine,
) -> None:
    factory = sessionmaker(bind=pg_engine)
    with factory() as setup:
        version = create_draft(setup, SERVICES, "pg-test").version
        setup.commit()

    barrier = threading.Barrier(2, timeout=15)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(session: Session) -> None:
        try:
            row = session.get(RulebookVersion, version)
            assert row is not None
            barrier.wait()
            publish(session, row)
            session.commit()
            outcome = "published"
        except ValueError:
            session.rollback()
            outcome = "conflict"
        finally:
            session.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(factory(),)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["conflict", "published"]


def test_concurrent_first_requests_seed_exactly_once(
    pg_engine: Engine,
) -> None:
    factory = sessionmaker(bind=pg_engine)
    barrier = threading.Barrier(2, timeout=15)

    def first_request() -> None:
        with factory() as session:
            barrier.wait()
            active_rulebook(session)
            session.commit()

    threads = [threading.Thread(target=first_request) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with factory() as session:
        seeds = (
            session.execute(
                select(RulebookVersion).where(RulebookVersion.author == "seed")
            )
            .scalars()
            .all()
        )
        assert len(seeds) == 1
