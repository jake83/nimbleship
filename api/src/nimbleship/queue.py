"""The Postgres-backed job queue (ADR 0004): Procrastinate tasks consumed
by the worker deployment, with jobs enqueued in the same transaction as
the domain writes that cause them. The queue lives in the database that
already exists - there is no broker.

The worker runs this module's app:

    procrastinate --app=nimbleship.queue.queue_app worker
"""

from procrastinate import App, PsycopgConnector, RetryStrategy
from procrastinate.job_context import JobContext
from sqlalchemy.orm import Session

from nimbleship.config import get_settings
from nimbleship.db import open_session
from nimbleship.domain.manifests import manifest_consignments, send_manifest
from nimbleship.engine.execute import CarrierCallError
from nimbleship.http_client import carrier_http_client
from nimbleship.models import Manifest, OrderEvent
from nimbleship.uploaders import carrier_uploaders

# Retries back off exponentially (6^n seconds: 6s, 36s, ~4m, ~22m, ~2h10m),
# long enough to ride out a carrier outage on the evening the trailer
# closes; after the final attempt the Manifest is marked failed for a human.
MANIFEST_RETRY = RetryStrategy(max_attempts=5, exponential_wait=6)


def _conninfo(database_url: str) -> str:
    # SQLAlchemy URLs name a driver (postgresql+psycopg://); psycopg wants
    # the plain scheme.
    scheme, _, rest = database_url.partition("://")
    return f"{scheme.partition('+')[0]}://{rest}"


queue_app = App(
    connector=PsycopgConnector(conninfo=_conninfo(get_settings().database_url))
)


def defer_manifest_send(session: Session, manifest_id: int) -> None:
    """Enqueue the send job on the session's own connection: the job INSERT
    commits or rolls back with the dispatch confirmation that caused it
    (ADR 0004) - the two can never disagree."""
    # The real connector defers over psycopg, so it needs a psycopg
    # connection: a session bound to anything but Postgres is a
    # misconfiguration (NIMBLESHIP_DATABASE_URL pointing at sqlite in a real
    # deployment). Stated as an invariant of the connector, not a test-mode
    # exemption - the in-memory connector the suite injects ignores the
    # connection entirely, so it is simply not a PsycopgConnector.
    if (
        isinstance(queue_app.connector, PsycopgConnector)
        and session.get_bind().dialect.name != "postgresql"
    ):
        raise RuntimeError(
            "background jobs enqueue into Postgres (ADR 0004); "
            "NIMBLESHIP_DATABASE_URL must point at a postgresql database"
        )
    connection = session.connection().connection.driver_connection
    send_manifest_job.configure(connection=connection).defer(manifest_id=manifest_id)


def run_manifest_send(manifest_id: int, attempts: int) -> None:
    """One send attempt, with its own session and carrier client (jobs run
    outside request scope). A failure updates the Manifest's bookkeeping -
    and marks it failed outright when no retries remain - then re-raises so
    the queue schedules the retry or records the permanent failure."""
    with open_session() as session:
        manifest = session.get(Manifest, manifest_id)
        if manifest is None:
            raise ValueError(f"no manifest {manifest_id}")
        # Derive the audit counter from the queue's own attempt count rather
        # than a hand-kept +=1: an at-least-once redelivery re-runs a job
        # with the same `attempts`, so the two can never drift.
        manifest.attempts = attempts + 1
        try:
            with carrier_http_client() as http_client:
                send_manifest(session, manifest, http_client, carrier_uploaders())
        # CarrierCallError and deterministic manifest errors - a ValueError
        # (missing render fact, or the manifest operation gone from the
        # definition between enqueue and run) or a NotImplementedError (a step
        # whose transport/content_type the engine cannot execute) - are all
        # human-fixable by editing the definition, not by retrying, so all mark
        # the Manifest failed rather than leaving it 'pending' forever. An
        # infrastructure fault (a DB error) is neither: it propagates so the
        # session rolls back instead of this handler committing a
        # rollback-required session and masking the real cause.
        except (CarrierCallError, ValueError, NotImplementedError) as error:
            manifest.last_error = str(error)
            final = (
                MANIFEST_RETRY.max_attempts is not None
                and attempts >= MANIFEST_RETRY.max_attempts
            )
            if final:
                manifest.status = "failed"
                for consignment in manifest_consignments(session, manifest):
                    session.add(
                        OrderEvent(
                            order_number=consignment.order_number,
                            stage="manifest_failed",
                            detail={
                                "manifest_id": manifest.id,
                                "carrier": manifest.carrier,
                                "error": str(error),
                            },
                        )
                    )
            # The attempt's audit trail (attempts, error, traffic) must
            # survive the raise that triggers the retry.
            session.commit()
            raise
        session.commit()


@queue_app.task(
    name="manifests.send",
    queue="manifests",
    pass_context=True,
    retry=MANIFEST_RETRY,
)
def send_manifest_job(context: JobContext, manifest_id: int) -> None:
    # context.job.attempts counts completed attempts, matching what
    # MANIFEST_RETRY will be asked about if this attempt fails.
    run_manifest_send(manifest_id, attempts=context.job.attempts)
