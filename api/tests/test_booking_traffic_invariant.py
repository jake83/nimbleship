"""Carrier contact always commits traffic: a booking that succeeds on the
carrier's side and then loses the duplicate-order race (IntegrityError ->
409) must keep its CarrierTraffic rows - a real carrier booking with no
audit trail is silent data loss (refuter, PR #30). A file-backed database
gives each session its own connection: the real transaction isolation the
suite's shared in-memory connection cannot produce."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from booking_race import (
    CONSIGNMENT_PAYLOAD,
    ORDER,
    build_app,
    publish_furdeco,
    racing_carrier,
)
from nimbleship.db import Base
from nimbleship.models import CarrierTraffic, Consignment


def test_traffic_survives_losing_the_duplicate_order_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIMBLESHIP_LABELS_DIR", str(tmp_path / "labels"))
    engine = create_engine(
        f"sqlite:///{tmp_path / 'race.db'}",
        # The route runs in the TestClient's worker thread; the racer and
        # the assertions below open their own connections. A short busy
        # timeout turns a lock regression into a fast, loud failure.
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    app = build_app(factory, tmp_path / "labels")

    try:
        with TestClient(app) as client:
            publish_furdeco(client)
            racing_carrier(app, factory)

            response = client.post("/api/consignments", json=CONSIGNMENT_PAYLOAD)

        assert response.status_code == 409

        with factory() as check:
            traffic = check.execute(select(CarrierTraffic)).scalars().all()
            assert [
                (t.carrier, t.order_number, t.step, t.response_status) for t in traffic
            ] == [("furdeco", ORDER, "save", 200)]
            survivors = check.execute(select(Consignment)).scalars().all()
            assert [c.recipient_name for c in survivors] == ["The Winner"]
    finally:
        engine.dispose()
