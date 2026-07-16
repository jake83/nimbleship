"""AllocationService operations (ADR 0011). allocateConsignments records the
requested service groups on the staged consignment and returns a synthetic
Allocated response; the real carrier allocation happens at paperwork."""

import xml.etree.ElementTree as ET

from sqlalchemy.orm import Session

from nimbleship.legacy import soap, staging


def handle(body: bytes, session: Session) -> bytes:
    request = soap.parse_request(body)
    if request.method == "allocateConsignments":
        return _allocate_consignments(request, session)
    raise soap.SoapFault(f"unsupported AllocationService operation '{request.method}'")


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
