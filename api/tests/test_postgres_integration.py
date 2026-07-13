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
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from booking_race import (
    CONSIGNMENT_PAYLOAD,
    ORDER,
    build_app,
    publish_furdeco,
    racing_carrier,
)
from nimbleship.db import Base
from nimbleship.domain.allocation import ServiceDeclaration
from nimbleship.domain.rulebook import active_rulebook, create_draft, publish
from nimbleship.models import CarrierTraffic, RulebookVersion

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


def test_booking_traffic_survives_losing_the_duplicate_order_race(
    pg_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Carrier contact always commits traffic, under real transaction
    isolation: a booking that succeeds on the carrier's side and then
    loses the duplicate-order race (IntegrityError -> 409) keeps its
    CarrierTraffic rows (refuter, PR #30)."""
    monkeypatch.setenv("NIMBLESHIP_LABELS_DIR", str(tmp_path / "labels"))
    factory = sessionmaker(bind=pg_engine)
    app = build_app(factory, tmp_path / "labels")

    def fail_fast(racer: Session) -> None:
        # A regression that holds the losing request's uncommitted
        # consignment row across the carrier call would block this
        # duplicate insert on the unique index until that transaction
        # ends - i.e. forever, since the request is waiting on us. A lock
        # timeout turns that hang into a loud failure.
        racer.execute(text("SET lock_timeout = '5s'"))

    with TestClient(app) as client:
        publish_furdeco(client)
        racing_carrier(app, factory, prepare_racer=fail_fast)

        response = client.post("/api/consignments", json=CONSIGNMENT_PAYLOAD)

    assert response.status_code == 409

    with factory() as check:
        traffic = check.execute(select(CarrierTraffic)).scalars().all()
        assert [
            (t.carrier, t.order_number, t.step, t.response_status) for t in traffic
        ] == [("furdeco", ORDER, "save", 200)]
