"""createPaperworkForConsignments (ADR 0011): the one lifecycle call that does
real work. It consumes the staged create+allocate data, runs the atomic domain
create-consignment, and translates the result into the paperwork response's
legacy obligations - the base64 label PDF and the Parcels String (CONTEXT.md).

The response is a single Paperwork return (one shipment per call), matching the
WMS's positional shape: documents (empty), the combined label PDF, then the
optional tracking reference and Parcels String. The SOAP-encoding type
decorations (xsi:type, encodingStyle) are not added yet; they byte-match against
the live WMS at shadow mode.

The order's Service Groups (ADR 0012) drive eligibility: the requested `custom1`
group unioned with the allocate call's accepted set become the domain's accepted
set. Delivery Proposition stays absent - it is the checkout caller's concept,
not the WMS's."""

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
from nimbleship.domain.service_groups import known_service_group_codes
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
        # partial-success response and per-code commit isolation, both deferred.
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
        # documents is always present but empty - the WMS reads labels, not it.
        documents = ET.SubElement(return_element, "documents")
        documents.set(f"{{{soap.XSI}}}nil", "true")
        soap.text_child(return_element, "labels", result.labels_base64)
        if result.tracking_reference is not None:
            soap.text_child(
                return_element, "trackingReference", result.tracking_reference
            )
        soap.text_child(return_element, "parcels", result.parcels)

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
    accepted_groups = _accepted_service_groups(
        created, row.allocation_data or {}, session
    )
    request = _consignment_request(created, accepted_groups)
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
        tracking_reference=consignment.tracking_reference,
        parcels=_parcels_string(
            request.order_number,
            # The carrier's own barcode when it reported one, else the Parcel
            # Barcode this system prints (as Drop Out, with no carrier barcode,
            # uses) - CONTEXT.md: Parcels String.
            [(p.sequence, p.carrier_barcode or p.barcode) for p in consignment.parcels],
        ),
        labels_base64=base64.b64encode(pdf).decode("ascii"),
    )


def _accepted_service_groups(
    created: dict[str, object], allocation: dict[str, object], session: Session
) -> list[str]:
    """The accepted Service Group set (ADR 0012): the requested group (`custom1`)
    unioned with the allocate call's accepted set. A legacy order must carry at
    least one group, and every code must be in the catalogue - faulting keeps a
    groupless or off-catalogue order from silently allocating unfiltered, and
    matches the WMS's own "no service group -> no services" behaviour."""
    requested = created.get("service_group")
    codes = {requested} if isinstance(requested, str) and requested else set()
    accepted = allocation.get("service_group_codes")
    if isinstance(accepted, list):
        codes.update(code for code in accepted if isinstance(code, str) and code)
    if not codes:
        raise soap.SoapFault(
            "createPaperworkForConsignments: the order carries no service group"
        )
    unknown = codes - known_service_group_codes(session)
    if unknown:
        raise soap.SoapFault(
            "createPaperworkForConsignments: unknown service group(s) "
            + ", ".join(sorted(unknown))
        )
    return sorted(codes)


def _consignment_request(
    created: dict[str, object], accepted_service_groups: list[str]
) -> ConsignmentRequest:
    parcels = created.get("parcels")
    parcel_list = parcels if isinstance(parcels, list) else []
    return ConsignmentRequest(
        order_number=str(created.get("order_number") or ""),
        recipient_name=str(created.get("recipient_name") or ""),
        address_lines=_string_list(created.get("address_lines")),
        postcode=str(created.get("postcode") or ""),
        destination_country=str(created.get("destination_country") or ""),
        # The Legacy Interface filters by Service Group (ADR 0012), not the
        # checkout-only Delivery Proposition; proposition stays absent.
        proposition=None,
        parcel_weights=[_weight(parcel) for parcel in parcel_list],
        max_dimension_cm=_max_dimension_cm(created),
        warehouse=_optional_str(created.get("warehouse")),
        force_service=None,
        accepted_service_groups=accepted_service_groups,
    )


def _max_dimension_cm(created: Mapping[str, object]) -> Decimal | None:
    """The consignment's largest single dimension. The WMS's consignment-level
    maxDimension is almost always the sentinel 0, so the real value is derived
    from the per-parcel dimensions; 0 or absent anywhere is treated as absent,
    and None means no dimension was supplied at all (optimistic, ADR 0007)."""
    candidates: list[Decimal] = []
    consignment = _positive_decimal(created.get("max_dimension_cm"))
    if consignment is not None:
        candidates.append(consignment)
    parcels = created.get("parcels")
    if isinstance(parcels, list):
        for parcel in parcels:
            if not isinstance(parcel, dict):
                continue
            for key in ("height_cm", "width_cm", "depth_cm"):
                dimension = _positive_decimal(parcel.get(key))
                if dimension is not None:
                    candidates.append(dimension)
    return max(candidates) if candidates else None


def _positive_decimal(value: object) -> Decimal | None:
    """Parse a WMS numeric: None for absent, the sentinel 0, or unparseable - a
    non-positive dimension means 'not provided', never a real zero."""
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return parsed if parsed > 0 else None


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
