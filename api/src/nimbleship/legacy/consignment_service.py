"""ConsignmentService operations (ADR 0011). createConsignments stages the
inbound shipment and returns a synthetic Unallocated response;
createPaperworkForConsignments runs the atomic domain create-consignment against
the accumulated create+allocate data."""

import xml.etree.ElementTree as ET
from collections.abc import Mapping

import httpx
from sqlalchemy.orm import Session

from nimbleship.labels.store import LabelStore
from nimbleship.legacy import paperwork_service, soap, staging
from nimbleship.models import ORDER_NUMBER_MAX
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
    if request.method == "createPaperworkForConsignments":
        return paperwork_service.create_paperwork(
            request, session, store, http_client, uploaders
        )
    raise soap.SoapFault(f"unsupported ConsignmentService operation '{request.method}'")


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
