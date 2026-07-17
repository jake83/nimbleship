"""Manifests (CONTEXT.md: Manifest): the per-carrier declaration of
consignments that have physically left the warehouse. Creation happens in
the dispatch-confirmation transaction; sending happens later, on a queue
worker, through the same integration engine as booking."""

from collections.abc import Mapping
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.domain.definitions import active_definition, carrier_config
from nimbleship.domain.facts import manifest_facts, shipment_facts, warehouse_facts
from nimbleship.engine.execute import StepRecord, execute_operation
from nimbleship.models import (
    CarrierTraffic,
    Consignment,
    Manifest,
    ManifestCode,
    ManifestConsignment,
    OrderEvent,
    Warehouse,
)
from nimbleship.uploaders import FileUploader


def ready_to_manifest(
    session: Session, carrier: str, warehouse: str | None
) -> list[Consignment]:
    """The consignments a mark-ready call left ready for this carrier and
    warehouse, in creation order - what a createManifest sweep closes over.
    Locks them for the sweep's transaction: two overlapping createManifest calls
    for the same carrier and warehouse would otherwise both read the same ready
    rows and declare them on two manifests, sending the carrier the same
    dispatch twice - the hazard the JSON dispatch-confirmation locks against too
    (a no-op on SQLite, real on the Postgres deployment)."""
    return list(
        session.execute(
            select(Consignment)
            .where(
                Consignment.carrier == carrier,
                Consignment.warehouse == warehouse,
                Consignment.status == "ready_to_manifest",
            )
            .order_by(Consignment.id)
            .with_for_update()
        )
        .scalars()
        .all()
    )


def mint_manifest_code(session: Session) -> str:
    """The next NS-native manifest code from its own sequence (ADR 0013), so a
    createManifest returns a valid code even when its sweep is empty and no
    Manifest row exists to derive one from."""
    row = ManifestCode()
    session.add(row)
    session.flush()  # assigns the id the code derives from
    return f"NSM{row.id:07d}"


def create_manifests(
    session: Session, consignments: list[Consignment]
) -> list[Manifest]:
    """Group the consignments into one pending Manifest per (carrier,
    warehouse) - for carriers whose published definition declares a manifest
    operation - and move them to "on_manifest"; the send is what dispatches
    them (ADR 0013). A consignment whose carrier has no manifest operation is
    dispatched here instead - only reachable if the definition dropped that
    operation after paperwork left it "allocated". The rows are flushed so
    callers can enqueue send jobs by manifest id in the same transaction
    (ADR 0004)."""
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
        assert consignment.carrier is not None
        manifest_for = manifest_by_group.get(
            (consignment.carrier, consignment.warehouse)
        )
        # On a manifest = pending its send, not yet gone. Without one, the
        # carrier does not manifest, so it is dispatched now (ADR 0013).
        stage = "on_manifest" if manifest_for is not None else "dispatched"
        consignment.status = stage
        session.add(
            OrderEvent(
                order_number=consignment.order_number,
                stage=stage,
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
    uploaders: Mapping[str, FileUploader],
) -> None:
    """Execute the carrier's manifest operation for this Manifest, recording
    the traffic (the golden corpus grows from manifests too). A fan_out
    manifest emits one document per consignment from that consignment's
    shipment facts; otherwise one document is rendered from the whole
    manifest's facts. Success marks the Manifest sent and appends a manifested
    event per consignment, each carrying its own send's extracted output; a
    carrier failure raises before anything is marked sent, with the traffic
    already recorded - the caller owns retry bookkeeping. Sending an
    already-sent Manifest is a no-op: the queue may redeliver a job whose work
    committed."""
    if manifest.status == "sent":
        return
    definition = active_definition(session, manifest.carrier)
    if definition is None or "manifest" not in definition.operations:
        raise ValueError(
            f"carrier '{manifest.carrier}' has no manifest operation in a "
            "published definition; it cannot be manifested"
        )
    consignments = manifest_consignments(session, manifest)
    config = carrier_config(session, manifest.carrier)
    warehouse_facts_value: dict[str, object] | None = None
    # No warehouse = UTC; a named warehouse must resolve, or the manifest would
    # go out missing its sender facts and misdated (its local day is unknown) -
    # a data-integrity failure the send raises on rather than sends silently.
    timezone = "UTC"
    if manifest.warehouse is not None:
        warehouse = session.execute(
            select(Warehouse).where(Warehouse.code == manifest.warehouse)
        ).scalar_one_or_none()
        if warehouse is None:
            raise ValueError(
                f"manifest {manifest.id} names warehouse '{manifest.warehouse}', "
                "which no longer exists; it cannot be sent"
            )
        warehouse_facts_value = warehouse_facts(warehouse)
        timezone = warehouse.timezone

    def run(facts: dict[str, object], traffic_key: str) -> dict[str, object]:
        # Each rendered document's traffic is keyed to what it declares: a
        # fan-out document is one order (its order_number), the single
        # whole-manifest document spans every order (the manifest).
        if warehouse_facts_value is not None:
            facts["warehouse"] = warehouse_facts_value

        def record(step_record: StepRecord) -> None:
            session.add(
                CarrierTraffic(
                    carrier=manifest.carrier,
                    order_number=traffic_key,
                    step=step_record.step,
                    request=step_record.request.model_dump(mode="json"),
                    response_status=step_record.response_status,
                    response_body=step_record.response_body,
                )
            )

        return execute_operation(
            definition, "manifest", facts, http_client, record, uploaders
        ).outputs

    operation = definition.operations["manifest"]
    emitted: list[tuple[Consignment, dict[str, object]]] = []
    if operation.fan_out:
        # One document per consignment, each rendered from that consignment's
        # own shipment facts (including the SSCCs stored at booking). A failure
        # partway raises before any consignment is marked manifested; the whole
        # manifest retries, and uploads are overwrite-idempotent so re-sending
        # the documents that already landed is safe.
        for consignment in consignments:
            outputs = run(
                {"shipment": shipment_facts(consignment), "config": config},
                consignment.order_number,
            )
            emitted.append((consignment, outputs))
    else:
        batch = run(
            {
                "manifest": manifest_facts(manifest, consignments, timezone),
                "config": config,
            },
            f"manifest-{manifest.id}",
        )
        emitted = [(consignment, batch) for consignment in consignments]

    manifest.status = "sent"
    manifest.sent_at = datetime.now(UTC)
    for consignment, outputs in emitted:
        # The manifest is away, so the goods have physically left: a manifest
        # carrier dispatches here, at send, not at manifest creation (ADR 0013).
        consignment.status = "dispatched"
        # The carrier's extracted outputs are author-named, so they live under
        # their own key: splatting them alongside the internal audit fields
        # would let an extraction named "manifest_id" overwrite the link back
        # to the Manifest row.
        extracted = {key: str(value) for key, value in outputs.items()}
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
