"""Postgres-only integration tests: the advisory-lock branch, the
migrations, and the job queue's enqueue-with-commit atomicity (ADR 0004),
exercised against a real server. Skipped unless
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
from nimbleship.domain.definitions import (
    carrier_config,
    merge_carrier_config,
    upsert_carrier_config,
)
from nimbleship.domain.rulebook import active_rulebook, create_draft, publish
from nimbleship.models import CarrierTraffic, RulebookVersion
from nimbleship.queue import defer_manifest_send

# The queue's schema objects (tables, functions, types) are Procrastinate's,
# created by the migration chain, so cleaning a test database means removing
# them too - they are not in Base.metadata.
DROP_PROCRASTINATE = """
DO $$
DECLARE
    name text;
    args text;
BEGIN
    FOR name IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = current_schema() AND tablename LIKE 'procrastinate_%'
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', name);
    END LOOP;
    FOR name, args IN
        SELECT p.proname, pg_get_function_identity_arguments(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = current_schema() AND p.proname LIKE 'procrastinate_%'
    LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %I(%s) CASCADE', name, args);
    END LOOP;
    FOR name IN
        SELECT t.typname FROM pg_type t JOIN pg_namespace n ON t.typnamespace = n.oid
        WHERE n.nspname = current_schema()
          AND t.typname LIKE 'procrastinate_%' AND t.typtype IN ('e', 'c')
    LOOP
        EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', name);
    END LOOP;
END
$$;
"""


def _clean_database(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS alembic_version"))
        connection.execute(text(DROP_PROCRASTINATE))


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
    _clean_database(engine)
    Base.metadata.create_all(engine)
    yield engine
    Base.metadata.drop_all(engine)
    engine.dispose()


def _alembic_config() -> Config:
    assert POSTGRES_URL is not None
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", POSTGRES_URL)
    return config


@pytest.fixture
def migrated_engine() -> "Iterator[Engine]":
    assert POSTGRES_URL is not None
    engine = create_engine(POSTGRES_URL)
    _clean_database(engine)
    command.upgrade(_alembic_config(), "head")
    yield engine
    _clean_database(engine)
    engine.dispose()


def test_alembic_upgrade_head_against_postgres(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())
    assert set(Base.metadata.tables) <= tables
    # The queue lives in the same database (ADR 0004): the migration chain
    # owns Procrastinate's schema too.
    assert "procrastinate_jobs" in tables


def test_manifest_jobs_enqueue_in_the_callers_transaction(
    migrated_engine: Engine,
) -> None:
    factory = sessionmaker(bind=migrated_engine)

    def queued_jobs() -> list[tuple[str, dict[str, object], str]]:
        # A separate connection: only committed jobs are visible to it,
        # which is exactly what the worker will see.
        with migrated_engine.connect() as connection:
            rows = connection.execute(
                text("SELECT task_name, args, status FROM procrastinate_jobs")
            ).all()
        return [(row.task_name, row.args, row.status) for row in rows]

    with factory() as session:
        defer_manifest_send(session, manifest_id=1)
        assert queued_jobs() == []  # not yet committed: invisible to the worker
        session.rollback()
    assert queued_jobs() == []  # rolled back: the job vanished with the writes

    with factory() as session:
        defer_manifest_send(session, manifest_id=2)
        session.commit()
    assert queued_jobs() == [("manifests.send", {"manifest_id": 2}, "todo")]


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


def test_concurrent_config_merges_do_not_lose_an_update(
    pg_engine: Engine,
) -> None:
    """Two PATCH /config merges rotating different single keys must both survive.
    merge_carrier_config is a read-modify-write; without the advisory lock the
    two would read the same row and the last commit would clobber the other's
    key. Barrier-synced so both are in flight together - the lock, not luck, is
    what makes both keys survive."""
    factory = sessionmaker(bind=pg_engine)
    with factory() as setup:
        upsert_carrier_config(
            setup, "racer", {"api_key": "K-0", "base_url": "https://b0.example"}
        )
        setup.commit()

    barrier = threading.Barrier(2, timeout=15)

    def rotate(key: str, value: str) -> None:
        with factory() as session:
            barrier.wait()
            merge_carrier_config(session, "racer", {key: value})
            session.commit()

    threads = [
        threading.Thread(target=rotate, args=("api_key", "K-1")),
        threading.Thread(target=rotate, args=("base_url", "https://b1.example")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with factory() as check:
        assert carrier_config(check, "racer") == {
            "api_key": "K-1",
            "base_url": "https://b1.example",
        }


def test_a_put_and_a_patch_config_serialise_without_a_lost_update(
    pg_engine: Engine,
) -> None:
    """A PUT (full replace) racing a PATCH (merge) on one carrier must serialise
    on the shared config-write lock: the PATCH must not merge onto a snapshot the
    PUT already replaced. Whichever orders first, the PUT's api_key survives - a
    torn interleave would write back the stale pre-PUT value instead."""
    factory = sessionmaker(bind=pg_engine)
    with factory() as setup:
        upsert_carrier_config(
            setup, "racer", {"api_key": "K-0", "base_url": "https://b0.example"}
        )
        setup.commit()

    barrier = threading.Barrier(2, timeout=15)

    def put_config() -> None:
        with factory() as session:
            barrier.wait()
            upsert_carrier_config(session, "racer", {"api_key": "PUT-KEY"})
            session.commit()

    def patch_config() -> None:
        with factory() as session:
            barrier.wait()
            merge_carrier_config(
                session, "racer", {"base_url": "https://patch.example"}
            )
            session.commit()

    threads = [
        threading.Thread(target=put_config),
        threading.Thread(target=patch_config),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with factory() as check:
        assert carrier_config(check, "racer")["api_key"] == "PUT-KEY"


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


# A carrier that manifests, for the dispatch-confirmation race test below.
MANIFEST_DEFINITION = {
    "carrier": "brightpost",
    "name": "Bright Post",
    "auth": {"scheme": "header_key", "header": "X-Api-Key", "secret": "config.api_key"},
    "operations": {
        "manifest": {
            "steps": [
                {
                    "name": "declare",
                    "transport": "http",
                    "request": {
                        "method": "POST",
                        "url": "config.manifest_url",
                        "content_type": "json",
                        "mapping": [{"target": "date", "source": "manifest.date"}],
                    },
                }
            ]
        }
    },
}


def test_concurrent_dispatch_confirmations_dispatch_a_consignment_once(
    migrated_engine: Engine,
) -> None:
    """Two overlapping confirmations for the same order must not both
    dispatch it and declare it on two manifests. The row lock in
    confirm_dispatch (with_for_update) is a no-op on SQLite, so only a real
    Postgres run under contention proves it: exactly one confirmation
    dispatches, the other is rejected, and exactly one Manifest exists.
    Uses the threading.Barrier pattern the publish-race test established."""
    from fastapi import HTTPException

    from nimbleship.models import (
        CarrierConfig,
        CarrierDefinitionVersion,
        Consignment,
        Manifest,
        Parcel,
    )
    from nimbleship.routers.manifests import DispatchConfirmationIn, confirm_dispatch

    factory = sessionmaker(bind=migrated_engine)
    with factory() as setup:
        setup.add(
            CarrierDefinitionVersion(
                carrier="brightpost",
                version=1,
                status="published",
                author="pg-test",
                data=MANIFEST_DEFINITION,
            )
        )
        setup.add(
            CarrierConfig(
                carrier="brightpost",
                data={"api_key": "K", "manifest_url": "https://api.brightpost/m"},
            )
        )
        consignment = Consignment(
            order_number="RACE-1",
            recipient_name="John Doe",
            address_lines=["10 Downing Street"],
            postcode="SW1A 2AA",
            destination_country="GB",
            status="allocated",
            carrier="brightpost",
            service="BP-STD",
            allocation={},
        )
        consignment.parcels = [Parcel(sequence=1, weight_kg="1", barcode="RACE-1-1")]
        setup.add(consignment)
        setup.commit()

    barrier = threading.Barrier(2, timeout=15)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(session: Session) -> None:
        try:
            barrier.wait()
            confirm_dispatch(DispatchConfirmationIn(order_numbers=["RACE-1"]), session)
            session.commit()
            outcome = "dispatched"
        except HTTPException:
            session.rollback()
            outcome = "rejected"
        finally:
            session.close()
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(factory(),)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes) == ["dispatched", "rejected"]
    with factory() as check:
        manifests = check.execute(select(Manifest)).scalars().all()
        assert len(manifests) == 1
