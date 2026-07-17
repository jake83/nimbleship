"""ConsignmentService operations (ADR 0011). createConsignments stages the
inbound shipment and returns a synthetic Unallocated response;
createPaperworkForConsignments runs the atomic domain create-consignment against
the accumulated create+allocate data."""

import xml.etree.ElementTree as ET
from collections.abc import Mapping

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.domain.consignments import LABELLED_STATUSES
from nimbleship.labels.store import LabelStore
from nimbleship.legacy import paperwork_service, soap, staging
from nimbleship.models import (
    ORDER_NUMBER_MAX,
    Consignment,
    LegacyConsignmentStaging,
    OrderEvent,
)
from nimbleship.uploaders import FileUploader


def handle(
    body: bytes,
    session: Session,
    store: LabelStore,
    http_client: httpx.Client,
    uploaders: Mapping[str, FileUploader],
) -> bytes:
    request = soap.parse_request(body)
    if request.method == "createConsignments":
        return _create_consignments(request, session)
    if request.method == "markConsignmentsAsReadyToManifest":
        return _mark_ready_to_manifest(request, session)
    if request.method == "markConsignmentsAsPrinted":
        return _mark_printed(request, session)
    if request.method == "deleteConsignment":
        return _delete_consignment()
    if request.method == "updateConsignments":
        return _update_consignments(request, session)
    if request.method == "createPaperworkForConsignments":
        return paperwork_service.create_paperwork(
            request, session, store, http_client, uploaders
        )
    raise soap.SoapFault(f"unsupported ConsignmentService operation '{request.method}'")


def _mark_ready_to_manifest(request: soap.SoapRequest, session: Session) -> bytes:
    """Selectively mark named consignments ready for a later manifest (ADR 0013):
    allocated -> ready_to_manifest. A no-op on an already-ready or already-
    dispatched consignment (a non-manifest carrier's, gone at paperwork) -
    matching the JSON dispatch-confirmation's allow-set, this is a manifest
    trigger, not an error. Any other status faults. Returns a bare boolean
    true."""
    codes = request.string_array(request.operation, "consignmentCodes")
    if not codes:
        raise soap.SoapFault("markConsignmentsAsReadyToManifest: no consignmentCodes")
    # Resolve and validate every code before mutating any, so one bad code faults
    # the whole batch rather than leaving it half-marked - the WMS must not be
    # left unsure which consignments it readied.
    to_ready: list[Consignment] = []
    for code in codes:
        if not code:
            raise soap.SoapFault(
                "markConsignmentsAsReadyToManifest: a blank consignmentCode"
            )
        consignment = _resolve_consignment(
            code, session, "markConsignmentsAsReadyToManifest"
        )
        # Already ready, or already dispatched at paperwork (a non-manifest
        # carrier's): a no-op, like the JSON edge's ("allocated", "dispatched")
        # allow-set, so a mixed batch is not hard-faulted (ADR 0013).
        if consignment.status in ("ready_to_manifest", "dispatched"):
            continue
        if consignment.status != "allocated":
            raise soap.SoapFault(
                f"markConsignmentsAsReadyToManifest: consignmentCode '{code}' "
                f"(status {consignment.status}) cannot be marked ready - only an "
                "allocated consignment can"
            )
        to_ready.append(consignment)
    for consignment in to_ready:
        consignment.status = "ready_to_manifest"
    session.flush()

    def build(operation_element: ET.Element) -> None:
        soap.text_child(
            operation_element, "markConsignmentsAsReadyToManifestReturn", "true"
        )

    return soap.response("markConsignmentsAsReadyToManifestResponse", build)


def _mark_printed(request: soap.SoapRequest, session: Session) -> bytes:
    """Record that the WMS printed labels for the named consignments: one
    "printed" event per consignment on the append-only timeline. Returns a bare
    boolean true; faults on an unknown code, like the other code-taking ops.
    Every code resolves before any event is written, so one bad code faults the
    whole batch. A code repeated in one call is one print run, so it records one
    event; a consignment with no label to print (a failed one) faults - printing
    it is not a fact that can be true."""
    codes = request.string_array(request.operation, "consignmentCodes")
    if not codes:
        raise soap.SoapFault("markConsignmentsAsPrinted: no consignmentCodes")
    printed: list[Consignment] = []
    seen: set[str] = set()
    for code in codes:
        if not code:
            raise soap.SoapFault("markConsignmentsAsPrinted: a blank consignmentCode")
        if code in seen:
            continue
        seen.add(code)
        consignment = _resolve_consignment(code, session, "markConsignmentsAsPrinted")
        if consignment.status not in LABELLED_STATUSES:
            raise soap.SoapFault(
                f"markConsignmentsAsPrinted: consignmentCode '{code}' "
                f"(status {consignment.status}) has no label to print"
            )
        printed.append(consignment)
    for consignment in printed:
        session.add(
            OrderEvent(
                order_number=consignment.order_number,
                stage="printed",
                detail={"carrier": consignment.carrier},
            )
        )
    session.flush()

    def build(operation_element: ET.Element) -> None:
        soap.text_child(operation_element, "markConsignmentsAsPrintedReturn", "true")

    return soap.response("markConsignmentsAsPrintedResponse", build)


def _delete_consignment() -> bytes:
    """A mock acknowledgement (bare true). Real cancellation - a cancelled
    status, reversing a carrier booking, guarding a dispatched consignment - is
    a deferred lifecycle epic; this satisfies the WMS's call shape without a
    state change. The WMS expects an unconditional success here."""

    def build(operation_element: ET.Element) -> None:
        soap.text_child(operation_element, "deleteConsignmentReturn", "true")

    return soap.response("deleteConsignmentResponse", build)


def _update_consignments(request: soap.SoapRequest, session: Session) -> bytes:
    """Amend staged consignments: replace each staging row's created_data with
    the resubmitted details so paperwork uses them, matched by order number (the
    staging key). Once a domain Consignment exists for the order (paperwork has
    run, whether it succeeded or failed) it is the record and a staged update
    cannot change it, so that is a success no-op. Faults on an unknown order.
    Returns the createConsignments shape."""
    array = request.follow_child(request.operation, "consignments")
    if array is None:
        raise soap.SoapFault("updateConsignments: no consignments element")
    # Same lock the create/allocate writes take: this read-modify-write shares
    # the staging table's one write concern, so an amend cannot lose its write to
    # a concurrent create resend for the same order.
    staging.serialise_staging_writes(session)
    updated: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for item in array.findall("Item"):
        consignment = request.follow(item)
        data = _consignment_data(request, consignment)
        order_number = data["order_number"]
        if not isinstance(order_number, str) or not order_number:
            raise soap.SoapFault("updateConsignments: a consignment has no orderNumber")
        if order_number in seen:
            raise soap.SoapFault(
                f"updateConsignments: duplicate orderNumber '{order_number}' in "
                "one batch"
            )
        seen.add(order_number)
        row = session.execute(
            select(LegacyConsignmentStaging).where(
                LegacyConsignmentStaging.order_number == order_number
            )
        ).scalar_one_or_none()
        if row is None:
            raise soap.SoapFault(
                f"updateConsignments: unknown orderNumber '{order_number}' - "
                "createConsignments must run first"
            )
        # A Consignment row in any state - including booking_failed/label_failed
        # - counts as paperworked and is the record, so the amend no-ops.
        papered = session.execute(
            select(Consignment.id).where(Consignment.order_number == order_number)
        ).scalar_one_or_none()
        if papered is None:
            row.created_data = data
        parcels = data["parcels"]
        parcel_count = len(parcels) if isinstance(parcels, list) else 0
        assert row.consignment_code is not None  # minted when the row was created
        updated.append((row.consignment_code, order_number, parcel_count))
    session.flush()

    def build(operation_element: ET.Element) -> None:
        return_element = ET.SubElement(operation_element, "updateConsignmentsReturn")
        for code, order_number, parcel_count in updated:
            item_element = ET.SubElement(return_element, "Item")
            soap.text_child(item_element, "consignmentCode", code)
            soap.text_child(item_element, "orderNumber", order_number)
            soap.text_child(item_element, "status", "Unallocated")
            soap.text_child(item_element, "parcelCount", str(parcel_count))

    return soap.response("updateConsignmentsResponse", build)


def _resolve_consignment(code: str, session: Session, operation: str) -> Consignment:
    row = session.execute(
        select(LegacyConsignmentStaging).where(
            LegacyConsignmentStaging.consignment_code == code
        )
    ).scalar_one_or_none()
    if row is None:
        raise soap.SoapFault(
            f"{operation}: unknown consignmentCode '{code}' - "
            "createConsignments must run first"
        )
    consignment = session.execute(
        select(Consignment).where(Consignment.order_number == row.order_number)
    ).scalar_one_or_none()
    if consignment is None:
        raise soap.SoapFault(
            f"{operation}: consignmentCode '{code}' has no paperwork yet - "
            "createPaperworkForConsignments must run first"
        )
    return consignment


def _create_consignments(request: soap.SoapRequest, session: Session) -> bytes:
    array = request.follow_child(request.operation, "consignments")
    if array is None:
        raise soap.SoapFault("createConsignments: no consignments element")
    staged: list[tuple[str, str, int]] = []
    seen_orders: set[str] = set()
    for item in array.findall("Item"):
        consignment = request.follow(item)
        data = _consignment_data(request, consignment)
        order_number = data["order_number"]
        # An order number keys the staging row and later becomes the domain
        # consignment; a create without one is faulted, not staged under a
        # "None" key that would collapse distinct shipments together.
        if not isinstance(order_number, str) or not order_number:
            raise soap.SoapFault("createConsignments: a consignment has no orderNumber")
        # The one field the edge length-checks: it is this call's staging key
        # (an indexed column), written before the domain validates the rest at
        # paperwork (ADR 0002 clarification). Uses the shared column constant.
        if len(order_number) > ORDER_NUMBER_MAX:
            raise soap.SoapFault(
                f"createConsignments: orderNumber exceeds {ORDER_NUMBER_MAX} characters"
            )
        # Two items in one batch sharing an order number are distinct shipments
        # colliding, not an idempotent resend of a whole call; faulted, so the
        # second does not silently overwrite the first's staging row and reuse
        # its code.
        if order_number in seen_orders:
            raise soap.SoapFault(
                f"createConsignments: duplicate orderNumber '{order_number}' in "
                "one batch"
            )
        seen_orders.add(order_number)
        parcels = data["parcels"]
        parcel_count = len(parcels) if isinstance(parcels, list) else 0
        code = staging.stage_created(session, data)
        staged.append((code, order_number, parcel_count))

    def build(operation_element: ET.Element) -> None:
        return_element = ET.SubElement(operation_element, "createConsignmentsReturn")
        for code, order_number, parcel_count in staged:
            item_element = ET.SubElement(return_element, "Item")
            soap.text_child(item_element, "consignmentCode", code)
            soap.text_child(item_element, "orderNumber", order_number)
            soap.text_child(item_element, "status", "Unallocated")
            soap.text_child(item_element, "parcelCount", str(parcel_count))

    return soap.response("createConsignmentsResponse", build)


def _consignment_data(
    request: soap.SoapRequest, consignment: ET.Element
) -> dict[str, object]:
    address = request.follow_child(consignment, "recipientAddress")
    parcels_array = request.follow_child(consignment, "parcels")
    parcels: list[dict[str, object]] = []
    if parcels_array is not None:
        for item in parcels_array.findall("Item"):
            parcel = request.follow(item)
            parcels.append(
                {
                    "number": parcel.findtext("number"),
                    "weight_kg": parcel.findtext("parcelWeight"),
                    # Dimensions feed the derived consignment max dimension; the
                    # WMS often sends them (and the consignment maxDimension) as 0.
                    "height_cm": parcel.findtext("parcelHeight"),
                    "width_cm": parcel.findtext("parcelWidth"),
                    "depth_cm": parcel.findtext("parcelDepth"),
                }
            )
    return {
        "order_number": consignment.findtext("orderNumber"),
        "recipient_name": consignment.findtext("recipientName"),
        "address_lines": _address_lines(address),
        "postcode": _child_text(address, "postCode"),
        "destination_country": _child_text(address, "countryCode"),
        "warehouse": consignment.findtext("senderCode"),
        "value": consignment.findtext("consignmentValue"),
        "max_dimension_cm": consignment.findtext("maxDimension"),
        "service_group": consignment.findtext("custom1"),
        "ioss_number": consignment.findtext("IOSSNumber"),
        "parcels": parcels,
    }


def _child_text(element: ET.Element | None, name: str) -> str | None:
    return None if element is None else element.findtext(name)


def _address_lines(address: ET.Element | None) -> list[str]:
    if address is None:
        return []
    lines = [address.findtext(f"line{n}") for n in (1, 2, 3, 4)]
    return [line for line in lines if line]
