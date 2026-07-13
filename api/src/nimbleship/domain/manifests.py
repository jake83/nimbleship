"""Manifests (CONTEXT.md: Manifest): the per-carrier declaration of
consignments that have physically left the warehouse. Creation happens in
the dispatch-confirmation transaction; sending happens later, on a queue
worker, through the same integration engine as booking."""

from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.domain.definitions import active_definition, carrier_config
from nimbleship.domain.facts import manifest_facts, warehouse_facts
from nimbleship.engine.execute import StepRecord, execute_operation
from nimbleship.ftp_client import FileUploader
from nimbleship.models import (
    CarrierTraffic,
    Consignment,
    Manifest,
    ManifestConsignment,
    OrderEvent,
    Warehouse,
)


def create_manifests(
    session: Session, consignments: list[Consignment]
) -> list[Manifest]:
    """Mark the consignments dispatched and group them into one pending
    Manifest per (carrier, warehouse) - for carriers whose published
    definition declares a manifest operation. A carrier without one simply
    does not manifest; its consignments are still dispatched. The rows are
    flushed so callers can enqueue send jobs by manifest id in the same
    transaction (ADR 0004)."""
    Group = tuple[str, str | None]
    groups: dict[Group, list[Consignment]] = {}
    for consignment in consignments:
        assert consignment.carrier is not None  # dispatchable means allocated
        groups.setdefault((consignment.carrier, consignment.warehouse), []).append(
            consignment
        )

    # The definition depends only on carrier; resolve each at most once even
    # when a carrier ships from several warehouses in one confirmation.
    manifests_by_carrier: dict[str, bool] = {}

    def manifests_carrier(carrier: str) -> bool:
        if carrier not in manifests_by_carrier:
            definition = active_definition(session, carrier)
            manifests_by_carrier[carrier] = (
                definition is not None and "manifest" in definition.operations
            )
        return manifests_by_carrier[carrier]

    manifests: list[Manifest] = []
    manifest_by_group: dict[Group, Manifest] = {}
    for (carrier, warehouse), group in groups.items():
        if not manifests_carrier(carrier):
            continue
        manifest = Manifest(carrier=carrier, warehouse=warehouse, status="pending")
        session.add(manifest)
        session.flush()
        for consignment in group:
            session.add(
                ManifestConsignment(
                    manifest_id=manifest.id, consignment_id=consignment.id
                )
            )
        manifest_by_group[(carrier, warehouse)] = manifest
        manifests.append(manifest)

    for consignment in consignments:
        consignment.status = "dispatched"
        assert consignment.carrier is not None
        manifest_for = manifest_by_group.get(
            (consignment.carrier, consignment.warehouse)
        )
        session.add(
            OrderEvent(
                order_number=consignment.order_number,
                stage="dispatched",
                detail={
                    "carrier": consignment.carrier,
                    "warehouse": consignment.warehouse,
                    "manifest_id": manifest_for.id if manifest_for else None,
                },
            )
        )
    session.flush()
    return manifests


def manifest_consignments(session: Session, manifest: Manifest) -> list[Consignment]:
    """The manifest's consignments in declaration order."""
    return list(
        session.execute(
            select(Consignment)
            .join(
                ManifestConsignment,
                ManifestConsignment.consignment_id == Consignment.id,
            )
            .where(ManifestConsignment.manifest_id == manifest.id)
            .order_by(ManifestConsignment.id)
        )
        .scalars()
        .all()
    )


def send_manifest(
    session: Session,
    manifest: Manifest,
    http_client: httpx.Client,
    uploader: FileUploader,
) -> None:
    """Execute the carrier's manifest operation for this Manifest, recording
    the traffic (the golden corpus grows from manifests too). Success marks
    the Manifest sent and appends a manifested event per consignment; a
    carrier failure raises with the traffic already recorded - the caller
    owns retry bookkeeping. Sending an already-sent Manifest is a no-op:
    the queue may redeliver a job whose work committed."""
    if manifest.status == "sent":
        return
    definition = active_definition(session, manifest.carrier)
    if definition is None or "manifest" not in definition.operations:
        raise ValueError(
            f"carrier '{manifest.carrier}' has no manifest operation in a "
            "published definition; it cannot be manifested"
        )
    consignments = manifest_consignments(session, manifest)
    facts: dict[str, object] = {
        "manifest": manifest_facts(manifest, consignments),
        "config": carrier_config(session, manifest.carrier),
    }
    if manifest.warehouse is not None:
        warehouse = session.execute(
            select(Warehouse).where(Warehouse.code == manifest.warehouse)
        ).scalar_one_or_none()
        if warehouse is not None:
            facts["warehouse"] = warehouse_facts(warehouse)

    def record(step_record: StepRecord) -> None:
        session.add(
            CarrierTraffic(
                carrier=manifest.carrier,
                # Manifests span orders; the traffic key is the manifest.
                order_number=f"manifest-{manifest.id}",
                step=step_record.step,
                request=step_record.request.model_dump(mode="json"),
                response_status=step_record.response_status,
                response_body=step_record.response_body,
            )
        )

    result = execute_operation(
        definition, "manifest", facts, http_client, record, uploader
    )

    manifest.status = "sent"
    manifest.sent_at = datetime.now(UTC)
    # The carrier's extracted outputs are author-named, so they live under
    # their own key: splatting them alongside the internal audit fields
    # would let an extraction named "manifest_id" overwrite the link back to
    # the Manifest row.
    extracted = {key: str(value) for key, value in result.outputs.items()}
    for consignment in consignments:
        session.add(
            OrderEvent(
                order_number=consignment.order_number,
                stage="manifested",
                detail={
                    "manifest_id": manifest.id,
                    "carrier": manifest.carrier,
                    "extracted": extracted,
                },
            )
        )
