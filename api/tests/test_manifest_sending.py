"""Sending a Manifest through the integration engine, worker-side: the
manifest operation renders from manifest.* facts, traffic is recorded for
Golden Replay, success marks the Manifest sent, and the job runner turns
carrier failures into retries - with the final attempt marking the
Manifest failed instead of losing it. All carrier HTTP is a MockTransport."""

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from procrastinate.jobs import Job
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from nimbleship.db import Base
from nimbleship.domain.manifests import send_manifest
from nimbleship.engine.execute import CarrierCallError
from nimbleship.models import (
    CarrierConfig,
    CarrierDefinitionVersion,
    CarrierTraffic,
    Consignment,
    Manifest,
    ManifestConsignment,
    OrderEvent,
    Parcel,
)
from nimbleship.queue import MANIFEST_RETRY, queue_app, run_manifest_send

DEFINITION: dict[str, object] = {
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
                        "mapping": [
                            {"target": "date", "source": "manifest.date"},
                            {"target": "count", "source": "manifest.consignment_count"},
                            {
                                "target": "orders",
                                "source": "manifest.consignments",
                                "each": [
                                    {"target": "order", "source": "item.order_number"},
                                    {
                                        "target": "reference",
                                        "source": "item.tracking_reference",
                                    },
                                ],
                            },
                        ],
                    },
                    "response": {
                        "format": "json",
                        "success_when": {"path": "manifest_id"},
                        "error_message": {"path": "error"},
                        "extract": [
                            {"name": "manifest_reference", "path": "manifest_id"}
                        ],
                    },
                }
            ]
        }
    },
}


FAN_OUT_DEFINITION: dict[str, object] = {
    "carrier": "brightpost",
    "name": "Bright Post",
    "auth": {"scheme": "header_key", "header": "X-Api-Key", "secret": "config.api_key"},
    "operations": {
        "manifest": {
            "fan_out": True,
            "steps": [
                {
                    "name": "declare",
                    "transport": "http",
                    "request": {
                        "method": "POST",
                        "url": "config.manifest_url",
                        "content_type": "json",
                        "mapping": [
                            {"target": "order", "source": "shipment.order_number"},
                            {"target": "postcode", "source": "shipment.postcode"},
                        ],
                    },
                    "response": {
                        "format": "json",
                        "success_when": {"path": "manifest_id"},
                        "error_message": {"path": "error"},
                        "extract": [
                            {"name": "manifest_reference", "path": "manifest_id"}
                        ],
                    },
                }
            ],
        }
    },
}


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    # A file-backed database: the job runner opens its own session from
    # settings, exactly as it will inside the worker process.
    url = f"sqlite:///{tmp_path / 'nimbleship.db'}"
    monkeypatch.setenv("NIMBLESHIP_DATABASE_URL", url)
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with sessionmaker(bind=engine)() as session:
        yield session


def _seed_manifest(session: Session, definition: dict[str, object]) -> Manifest:
    session.add(
        CarrierDefinitionVersion(
            carrier="brightpost",
            version=1,
            status="published",
            author="test",
            data=definition,
        )
    )
    session.add(
        CarrierConfig(
            carrier="brightpost",
            data={
                "api_key": "SECRET-KEY",
                "manifest_url": "https://api.brightpost.example/manifests",
                "ftp_remote_dir": "/outbound",
            },
        )
    )
    consignments = []
    for i, order in enumerate(("O-1", "O-2"), start=1):
        consignment = Consignment(
            order_number=order,
            recipient_name="John Doe",
            address_lines=["10 Downing Street"],
            postcode="SW1A 2AA",
            destination_country="GB",
            status="dispatched",
            carrier="brightpost",
            service="BP-STD",
            tracking_reference=f"BP{i}0001",
            allocation={},
        )
        consignment.parcels = [
            Parcel(sequence=1, weight_kg="4.2", barcode=f"{order}-1")
        ]
        session.add(consignment)
        consignments.append(consignment)
    manifest = Manifest(carrier="brightpost", status="pending")
    session.add(manifest)
    session.flush()
    for consignment in consignments:
        session.add(
            ManifestConsignment(manifest_id=manifest.id, consignment_id=consignment.id)
        )
    session.flush()
    return manifest


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


SUCCESS = {"manifest_id": "MAN-77"}


def test_sending_declares_the_consignments_and_marks_the_manifest_sent(
    session: Session,
) -> None:
    manifest = _seed_manifest(session, DEFINITION)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SUCCESS)

    with _client(handler) as http_client:
        send_manifest(session, manifest, http_client, {})

    [request] = requests
    assert request.headers["X-Api-Key"] == "SECRET-KEY"
    body = json.loads(request.content)
    assert body["date"] == manifest.created_at.date().isoformat()
    assert body["count"] == "2"
    assert body["orders"] == [
        {"order": "O-1", "reference": "BP10001"},
        {"order": "O-2", "reference": "BP20001"},
    ]

    assert manifest.status == "sent"
    assert manifest.sent_at is not None

    events = session.execute(select(OrderEvent).order_by(OrderEvent.id)).scalars().all()
    assert [(e.order_number, e.stage) for e in events] == [
        ("O-1", "manifested"),
        ("O-2", "manifested"),
    ]
    # The carrier's extracted outputs live under their own key so an
    # author-chosen name can never shadow the internal audit fields.
    for event in events:
        extracted = event.detail["extracted"]
        assert isinstance(extracted, dict)
        assert extracted["manifest_reference"] == "MAN-77"
        assert event.detail["manifest_id"] == manifest.id


FTP_MANIFEST_DEFINITION: dict[str, object] = {
    "carrier": "brightpost",
    "name": "Bright Post",
    "auth": {"scheme": "none"},
    "operations": {
        "manifest": {
            "steps": [
                {
                    "name": "drop",
                    "transport": "ftp_upload",
                    "request": {
                        "url": "config.ftp_remote_dir",
                        "filename": "manifest-{manifest.date}.csv",
                        "content_type": "csv",
                        "mapping": [
                            {"target": "date", "source": "manifest.date"},
                            {"target": "count", "source": "manifest.consignment_count"},
                        ],
                    },
                }
            ]
        }
    },
}


class _RecordingUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def upload(
        self,
        config: dict[str, object],
        remote_path: str,
        filename: str,
        content: str,
    ) -> None:
        self.calls.append((remote_path, filename, content))


def test_a_manifest_over_ftp_is_routed_to_the_uploader(session: Session) -> None:
    # A carrier whose manifest is a file drop must reach the uploader, not
    # RuntimeError for a missing one (the queue path wires it in).
    manifest = _seed_manifest(session, FTP_MANIFEST_DEFINITION)
    uploader = _RecordingUploader()

    with _client(lambda request: httpx.Response(500)) as http_client:
        send_manifest(session, manifest, http_client, {"ftp_upload": uploader})

    [(remote_path, filename, content)] = uploader.calls
    assert remote_path == "/outbound"
    assert filename == f"manifest-{manifest.created_at.date().isoformat()}.csv"
    assert content == f"{manifest.created_at.date().isoformat()},2\r\n"
    assert manifest.status == "sent"


def test_sending_records_the_traffic_for_golden_replay(session: Session) -> None:
    manifest = _seed_manifest(session, DEFINITION)

    with _client(lambda request: httpx.Response(200, json=SUCCESS)) as http_client:
        send_manifest(session, manifest, http_client, {})

    [row] = session.execute(select(CarrierTraffic)).scalars().all()
    assert row.carrier == "brightpost"
    assert row.order_number == f"manifest-{manifest.id}"
    assert row.step == "declare"
    assert row.response_status == 200
    assert "MAN-77" in row.response_body


def test_a_carrier_error_raises_and_still_records_the_traffic(
    session: Session,
) -> None:
    manifest = _seed_manifest(session, DEFINITION)
    error = {"error": "manifest window closed"}

    with (
        _client(lambda request: httpx.Response(200, json=error)) as http_client,
        pytest.raises(CarrierCallError, match="manifest window closed"),
    ):
        send_manifest(session, manifest, http_client, {})

    assert manifest.status == "pending"
    assert manifest.sent_at is None
    [row] = session.execute(select(CarrierTraffic)).scalars().all()
    assert "manifest window closed" in row.response_body


def test_a_fan_out_manifest_sends_one_document_per_consignment(
    session: Session,
) -> None:
    manifest = _seed_manifest(session, FAN_OUT_DEFINITION)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"manifest_id": f"MAN-{len(requests)}"})

    with _client(handler) as http_client:
        send_manifest(session, manifest, http_client, {})

    # One document per consignment, each rendered from its own shipment facts.
    assert [json.loads(r.content)["order"] for r in requests] == ["O-1", "O-2"]
    assert manifest.status == "sent"
    assert manifest.sent_at is not None

    events = session.execute(select(OrderEvent).order_by(OrderEvent.id)).scalars().all()
    assert [(e.order_number, e.stage) for e in events] == [
        ("O-1", "manifested"),
        ("O-2", "manifested"),
    ]
    # Each consignment's event carries its own send's extracted output.
    references = {
        e.order_number: e.detail["extracted"]["manifest_reference"]  # type: ignore[index]
        for e in events
    }
    assert references == {"O-1": "MAN-1", "O-2": "MAN-2"}


def test_a_fan_out_failure_partway_raises_and_leaves_the_manifest_pending(
    session: Session,
) -> None:
    manifest = _seed_manifest(session, FAN_OUT_DEFINITION)

    def handler(request: httpx.Request) -> httpx.Response:
        order = json.loads(request.content)["order"]
        # The second consignment's send is rejected (no manifest_id).
        if order == "O-2":
            return httpx.Response(200, json={"error": "rejected"})
        return httpx.Response(200, json={"manifest_id": "MAN-1"})

    with (
        _client(handler) as http_client,
        pytest.raises(CarrierCallError, match="rejected"),
    ):
        send_manifest(session, manifest, http_client, {})

    # The manifest stays pending for retry, and no consignment is marked
    # manifested when the fan-out did not complete - the whole manifest retries
    # (uploads are overwrite-idempotent).
    assert manifest.status == "pending"
    assert manifest.sent_at is None
    assert session.execute(select(OrderEvent)).scalars().all() == []


def test_an_already_sent_manifest_is_not_sent_again(session: Session) -> None:
    manifest = _seed_manifest(session, DEFINITION)
    manifest.status = "sent"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("a sent manifest must never reach the carrier again")

    with _client(handler) as http_client:
        send_manifest(session, manifest, http_client, {})

    assert manifest.status == "sent"


def test_a_definition_without_a_manifest_operation_fails_loudly(
    session: Session,
) -> None:
    definition: dict[str, object] = {
        "carrier": "brightpost",
        "name": "Bright Post",
        "auth": DEFINITION["auth"],
        "operations": {
            "book": {"steps": [], "label": {"source": "local_render"}},
        },
    }
    manifest = _seed_manifest(session, definition)

    with (
        _client(lambda request: httpx.Response(200)) as http_client,
        pytest.raises(ValueError, match="manifest"),
    ):
        send_manifest(session, manifest, http_client, {})


def test_the_job_runner_marks_a_deterministic_error_failed_not_stuck_pending(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A definition losing its manifest operation between enqueue and run is
    # a deterministic ValueError, not a carrier failure - retrying cannot
    # help. It must still mark the Manifest failed for a human on the final
    # attempt, never leave it 'pending' forever.
    definition: dict[str, object] = {
        "carrier": "brightpost",
        "name": "Bright Post",
        "auth": DEFINITION["auth"],
        "operations": {
            "book": {"steps": [], "label": {"source": "local_render"}},
        },
    }
    manifest = _seed_manifest(session, definition)
    session.commit()
    monkeypatch.setattr(
        "nimbleship.queue.carrier_http_client",
        lambda: _client(lambda request: httpx.Response(200)),
    )
    assert MANIFEST_RETRY.max_attempts is not None

    with pytest.raises(ValueError, match="manifest"):
        run_manifest_send(manifest.id, attempts=MANIFEST_RETRY.max_attempts)

    with sessionmaker(bind=engine)() as fresh:
        row = fresh.get(Manifest, manifest.id)
        assert row is not None
        assert row.status == "failed"
        assert row.last_error is not None
        stages = fresh.execute(select(OrderEvent.stage)).scalars().all()
        assert stages.count("manifest_failed") == 2


def test_the_job_runner_keeps_the_manifest_pending_while_retries_remain(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _seed_manifest(session, DEFINITION)
    session.commit()
    error = {"error": "manifest window closed"}
    monkeypatch.setattr(
        "nimbleship.queue.carrier_http_client",
        lambda: _client(lambda request: httpx.Response(200, json=error)),
    )

    with pytest.raises(CarrierCallError):
        run_manifest_send(manifest.id, attempts=0)

    with sessionmaker(bind=engine)() as fresh:
        row = fresh.get(Manifest, manifest.id)
        assert row is not None
        assert row.status == "pending"
        assert row.attempts == 1
        assert row.last_error == "manifest window closed"
        stages = fresh.execute(select(OrderEvent.stage)).scalars().all()
        assert "manifest_failed" not in stages


def test_the_job_runner_marks_the_manifest_failed_on_the_final_attempt(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _seed_manifest(session, DEFINITION)
    session.commit()
    error = {"error": "manifest window closed"}
    monkeypatch.setattr(
        "nimbleship.queue.carrier_http_client",
        lambda: _client(lambda request: httpx.Response(200, json=error)),
    )
    assert MANIFEST_RETRY.max_attempts is not None

    with pytest.raises(CarrierCallError):
        run_manifest_send(manifest.id, attempts=MANIFEST_RETRY.max_attempts)

    with sessionmaker(bind=engine)() as fresh:
        row = fresh.get(Manifest, manifest.id)
        assert row is not None
        assert row.status == "failed"
        assert row.last_error == "manifest window closed"
        events = (
            fresh.execute(
                select(OrderEvent).where(OrderEvent.stage == "manifest_failed")
            )
            .scalars()
            .all()
        )
        assert sorted(e.order_number for e in events) == ["O-1", "O-2"]
        assert all(e.detail["error"] == "manifest window closed" for e in events)


def test_the_job_runner_succeeds_end_to_end(
    engine: Engine,
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _seed_manifest(session, DEFINITION)
    session.commit()
    monkeypatch.setattr(
        "nimbleship.queue.carrier_http_client",
        lambda: _client(lambda request: httpx.Response(200, json=SUCCESS)),
    )

    run_manifest_send(manifest.id, attempts=0)

    with sessionmaker(bind=engine)() as fresh:
        row = fresh.get(Manifest, manifest.id)
        assert row is not None
        assert row.status == "sent"
        assert row.attempts == 1


def test_the_send_task_is_registered_with_bounded_retries() -> None:
    task = queue_app.tasks["manifests.send"]
    assert task.retry_strategy is MANIFEST_RETRY
    assert MANIFEST_RETRY.max_attempts is not None
    assert MANIFEST_RETRY.max_attempts >= 3


def test_the_final_attempt_boundary_agrees_with_procrastinate() -> None:
    """run_manifest_send marks a Manifest failed on the attempt where the
    queue itself stops retrying - not one too early (a live manifest wrongly
    failed) nor one too late (the queue gives up while the Manifest stays
    'pending' forever, the exact bug the design prevents). Both decisions
    read the same value - context.job.attempts - so pin that they flip
    together: this ties `final = attempts >= max_attempts` to
    Procrastinate's own `job.attempts >= max_attempts` give-up rule, in CI,
    without the ~2.5h of real backoff a live worker loop would take."""
    max_attempts = MANIFEST_RETRY.max_attempts
    assert max_attempts is not None

    def queue_gives_up(attempts: int) -> bool:
        job = Job(
            queue="manifests",
            lock=None,
            queueing_lock=None,
            task_name="manifests.send",
            attempts=attempts,
        )
        return (
            MANIFEST_RETRY.get_retry_decision(exception=Exception("boom"), job=job)
            is None
        )

    def run_marks_final(attempts: int) -> bool:
        # The boundary from run_manifest_send, evaluated on the same value.
        return attempts >= max_attempts

    # They flip at exactly the same attempts value: give up and mark failed
    # on the final attempt, retry (and stay pending) one attempt earlier.
    assert queue_gives_up(max_attempts) and run_marks_final(max_attempts)
    assert not queue_gives_up(max_attempts - 1)
    assert not run_marks_final(max_attempts - 1)
