"""AllocationService operations (ADR 0011). allocateConsignments records the
requested service groups on the staged consignment and returns a synthetic
Allocated response; the real carrier allocation happens at paperwork."""

import xml.etree.ElementTree as ET

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.legacy import soap, staging
from nimbleship.models import Consignment, LegacyConsignmentStaging


def handle(body: bytes, session: Session) -> bytes:
    request = soap.parse_request(body)
    if request.method == "allocateConsignments":
        return _allocate_consignments(request, session)
    if request.method == "deallocate":
        return _deallocate(request, session)
    raise soap.SoapFault(f"unsupported AllocationService operation '{request.method}'")


def _deallocate(request: soap.SoapRequest, session: Session) -> bytes:
    """Undo a staged allocation: clear the staging row's allocation_data so the
    consignment reverts to created-but-unallocated and can be re-allocated. Once
    a domain Consignment exists for the order (paperwork has run, whether it
    succeeded or failed) it owns the allocation and a staged clear cannot reverse
    it, so that is a success no-op - the WMS expects success either way. Faults
    on an unknown code. Returns the codes."""
    codes = request.string_array(request.operation, "consignmentCodes")
    if not codes:
        raise soap.SoapFault("deallocate: no consignmentCodes")
    # Same lock the create/allocate writes take: this read-modify-write shares
    # the staging table's one write concern, so it cannot lose its clear to a
    # concurrent allocate rewriting the same row (staging.serialise_staging_writes).
    staging.serialise_staging_writes(session)
    seen: set[str] = set()
    for code in codes:
        if not code:
            raise soap.SoapFault("deallocate: a blank consignmentCode")
        if code in seen:
            raise soap.SoapFault(
                f"deallocate: duplicate consignmentCode '{code}' in one batch"
            )
        seen.add(code)
        row = session.execute(
            select(LegacyConsignmentStaging).where(
                LegacyConsignmentStaging.consignment_code == code
            )
        ).scalar_one_or_none()
        if row is None:
            raise soap.SoapFault(
                f"deallocate: unknown consignmentCode '{code}' - "
                "createConsignments must run first"
            )
        # A Consignment row in any state - including booking_failed/label_failed
        # - counts as paperworked and owns the allocation, so deallocate no-ops.
        papered = session.execute(
            select(Consignment.id).where(Consignment.order_number == row.order_number)
        ).scalar_one_or_none()
        if papered is None:
            row.allocation_data = None
    session.flush()

    def build(operation_element: ET.Element) -> None:
        return_element = ET.SubElement(operation_element, "deallocateReturn")
        for code in codes:
            soap.text_child(return_element, "Item", code)

    return soap.response("deallocateResponse", build)


def _allocate_consignments(request: soap.SoapRequest, session: Session) -> bytes:
    codes = request.string_array(request.operation, "consignmentCodes")
    if not codes:
        raise soap.SoapFault("allocateConsignments: no consignmentCodes")
    # The requested service groups (e.g. NEXTDAY) are staged raw here and
    # consumed as the Service Group accepted set at paperwork (ADR 0012).
    service_groups = _service_groups(request)
    seen: set[str] = set()
    for code in codes:
        # A blank Item is a code the WMS sent but did not fill; fault rather than
        # drop it, or the WMS believes a shipment it never named was allocated.
        if not code:
            raise soap.SoapFault("allocateConsignments: a blank consignmentCode")
        if code in seen:
            raise soap.SoapFault(
                f"allocateConsignments: duplicate consignmentCode '{code}' in one batch"
            )
        seen.add(code)
        allocated = staging.stage_allocation(
            session, code, {"service_group_codes": service_groups}
        )
        # A code only exists once create minted it, so an unknown one means the
        # WMS is allocating something never created - faulted, not silently
        # allocated against nothing (ADR 0011).
        if not allocated:
            raise soap.SoapFault(
                f"allocateConsignments: unknown consignmentCode '{code}' - "
                "createConsignments must run first"
            )

    def build(operation_element: ET.Element) -> None:
        return_element = ET.SubElement(operation_element, "allocateConsignmentsReturn")
        for code in codes:
            item_element = ET.SubElement(return_element, "Item")
            soap.text_child(item_element, "consignmentCode", code)
            soap.text_child(item_element, "status", "Allocated")

    return soap.response("allocateConsignmentsResponse", build)


def _service_groups(request: soap.SoapRequest) -> list[str]:
    filter_element = request.follow_child(request.operation, "filter")
    if filter_element is None:
        return []
    # Blank group codes are noise (unlike a blank consignment code); drop them.
    return [
        group
        for group in request.string_array(
            filter_element, "acceptableCarrierServiceGroupCodes"
        )
        if group
    ]
