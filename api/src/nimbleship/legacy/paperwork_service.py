"""createPaperworkForConsignments (ADR 0011): the one lifecycle call that does
real work. It consumes the staged create+allocate data, runs the atomic domain
create-consignment, and translates the result into the paperwork response's
legacy obligations - the base64 label PDF and the Parcels String (CONTEXT.md).

The staged allocate intent (service groups) is not yet mapped to a Delivery
Proposition, so the domain runs unfiltered (proposition=None); the
serviceGroup->proposition table and byte-exact response fidelity are the
Phase 4 grilling items, deferred to a later slice."""

import base64
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.domain.consignments import (
    ConsignmentError,
    ConsignmentRequest,
    create_consignment,
)
from nimbleship.labels.store import LabelStore
from nimbleship.legacy import soap
from nimbleship.models import LegacyConsignmentStaging
from nimbleship.uploaders import FileUploader

# The Parcels String wire format (CONTEXT.md): comma-joined
# `{order}-parcel-{n}:{barcode}`, `{n}` the 1-based print sequence.
_PARCEL_DELIMITER = "-parcel-"
_TRACKING_SEPARATOR = ":"
_TRACKING_LIST_SEPARATOR = ","


@dataclass
class _Paperwork:
    code: str
    tracking_reference: str | None
    parcels: str
    labels_base64: str


def create_paperwork(
    request: soap.SoapRequest,
    session: Session,
    store: LabelStore,
    http_client: httpx.Client,
    uploaders: Mapping[str, FileUploader],
) -> bytes:
    codes = request.string_array(request.operation, "consignmentCodes")
    if not codes:
        raise soap.SoapFault("createPaperworkForConsignments: no consignmentCodes")
    if len(codes) > 1:
        # One shipment per call. create_consignment commits the request session
        # on its own failure paths (a failed booking's audit trail must survive
        # the fault), so a second code booking after a first would commit the
        # first's shipment even as the call faults - stranding a real carrier
        # booking the WMS is never told about. Safe batching needs a
        # partial-success response and per-code commit isolation, which wait for
        # the real recorded paperwork response shape (a single Paperwork return).
        raise soap.SoapFault(
            "createPaperworkForConsignments supports one consignmentCode per call"
        )
    code = codes[0]
    if not code:
        raise soap.SoapFault("createPaperworkForConsignments: a blank consignmentCode")
    row = _staged_row(session, code)
    result = _produce(row, store, http_client, uploaders, session)

    def build(operation_element: ET.Element) -> None:
        return_element = ET.SubElement(
            operation_element, "createPaperworkForConsignmentsReturn"
        )
        item = ET.SubElement(return_element, "Item")
        soap.text_child(item, "consignmentCode", result.code)
        if result.tracking_reference is not None:
            soap.text_child(item, "trackingReference", result.tracking_reference)
        soap.text_child(item, "parcels", result.parcels)
        soap.text_child(item, "labels", result.labels_base64)

    return soap.response("createPaperworkForConsignmentsResponse", build)


def _staged_row(session: Session, code: str) -> LegacyConsignmentStaging:
    row = session.execute(
        select(LegacyConsignmentStaging).where(
            LegacyConsignmentStaging.consignment_code == code
        )
    ).scalar_one_or_none()
    if row is None:
        raise soap.SoapFault(
            f"createPaperworkForConsignments: unknown consignmentCode '{code}' - "
            "createConsignments must run first"
        )
    # Strict lifecycle ordering (ADR 0011): paperwork reads the allocate call's
    # stored intent, so a code created but never allocated is a lifecycle error.
    if row.allocation_data is None:
        raise soap.SoapFault(
            f"createPaperworkForConsignments: consignmentCode '{code}' is not "
            "allocated - allocateConsignments must run first"
        )
    return row


def _produce(
    row: LegacyConsignmentStaging,
    store: LabelStore,
    http_client: httpx.Client,
    uploaders: Mapping[str, FileUploader],
    session: Session,
) -> _Paperwork:
    created = row.created_data or {}
    request = _consignment_request(created)
    try:
        result = create_consignment(session, request, store, http_client, uploaders)
    except ConsignmentError as error:
        raise soap.SoapFault(
            f"createPaperworkForConsignments: {request.order_number}: {error.detail}"
        ) from error
    consignment = result.consignment
    if consignment.status != "allocated":
        # No carrier could serve the shipment, so there is no label to return;
        # the WMS is told loudly rather than handed an empty paperwork response.
        raise soap.SoapFault(
            f"createPaperworkForConsignments: {request.order_number} could not be "
            f"allocated ({result.allocation.reason})"
        )
    pdf = store.load(request.order_number)
    if pdf is None:
        raise soap.SoapFault(
            f"createPaperworkForConsignments: {request.order_number} produced no label"
        )
    return _Paperwork(
        code=str(row.consignment_code),
        tracking_reference=consignment.tracking_reference,
        parcels=_parcels_string(
            request.order_number,
            [(p.sequence, p.barcode) for p in consignment.parcels],
        ),
        labels_base64=base64.b64encode(pdf).decode("ascii"),
    )


def _consignment_request(created: dict[str, object]) -> ConsignmentRequest:
    parcels = created.get("parcels")
    parcel_list = parcels if isinstance(parcels, list) else []
    return ConsignmentRequest(
        order_number=str(created.get("order_number") or ""),
        recipient_name=str(created.get("recipient_name") or ""),
        address_lines=_string_list(created.get("address_lines")),
        postcode=str(created.get("postcode") or ""),
        destination_country=str(created.get("destination_country") or ""),
        # Deferred: the staged service groups map to a Delivery Proposition via a
        # grilling-item table not built yet, so dispatch runs unfiltered for now.
        proposition=None,
        parcel_weights=[_weight(parcel) for parcel in parcel_list],
        warehouse=_optional_str(created.get("warehouse")),
        force_service=None,
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _weight(parcel: object) -> Decimal:
    weight = parcel.get("weight_kg") if isinstance(parcel, dict) else None
    try:
        return Decimal(str(weight))
    except (InvalidOperation, TypeError) as error:
        raise soap.SoapFault(
            f"createPaperworkForConsignments: a parcel weight '{weight}' is not a "
            "number"
        ) from error


def _parcels_string(order_number: str, parcels: list[tuple[int, str]]) -> str:
    return _TRACKING_LIST_SEPARATOR.join(
        f"{order_number}{_PARCEL_DELIMITER}{sequence}{_TRACKING_SEPARATOR}{barcode}"
        for sequence, barcode in parcels
    )
