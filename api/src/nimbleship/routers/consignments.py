from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.config import get_settings
from nimbleship.db import get_session
from nimbleship.domain import consignments as consignments_domain
from nimbleship.domain.allocation import AllocationResult
from nimbleship.domain.consignments import (
    LABELLED_STATUSES,
    ConsignmentError,
    ConsignmentRequest,
    create_consignment,
)
from nimbleship.http_client import get_http_client
from nimbleship.labels.store import LabelStore, get_label_store
from nimbleship.models import (
    COUNTRY_CODE_MAX,
    ORDER_NUMBER_MAX,
    POSTCODE_MAX,
    RECIPIENT_NAME_MAX,
    Consignment,
    OrderEvent,
)
from nimbleship.uploaders import FileUploader, get_carrier_uploaders

router = APIRouter(prefix="/consignments", tags=["consignments"])

SessionDep = Annotated[Session, Depends(get_session)]
LabelStoreDep = Annotated[LabelStore, Depends(get_label_store)]
HttpClientDep = Annotated[httpx.Client, Depends(get_http_client)]
UploaderDep = Annotated[Mapping[str, FileUploader], Depends(get_carrier_uploaders)]


class ParcelIn(BaseModel):
    weight_kg: Decimal = Field(gt=0)


class ConsignmentIn(BaseModel):
    # ASCII only: order numbers become Code 128 barcodes, whose encoder
    # rejects anything outside Latin-1 (refuter finding, PR #6). Length caps
    # mirror models.py's shared constants.
    order_number: str = Field(
        min_length=1, max_length=ORDER_NUMBER_MAX, pattern=r"^[A-Za-z0-9_-]+$"
    )
    recipient_name: str = Field(max_length=RECIPIENT_NAME_MAX)
    address_lines: list[str]
    postcode: str = Field(max_length=POSTCODE_MAX)
    destination_country: str = Field(min_length=2, max_length=COUNTRY_CODE_MAX)
    # The Delivery Proposition the customer bought (CONTEXT.md); dispatch
    # selects only among services fulfilling it. None = no filter.
    proposition: str | None = Field(default=None, min_length=1, max_length=64)
    parcels: list[ParcelIn] = Field(min_length=1)
    # The Warehouse code the consignment dispatches from (CONTEXT.md:
    # Warehouse - a logical dispatch identity); it supplies the label's
    # sender details.
    warehouse: str | None = Field(default=None, max_length=64)
    # Testing tools only (403 in production): pins the allocation to one
    # service, bypassing selection but not the audit trail.
    force_service: str | None = Field(default=None, max_length=64)


class ConsignmentOut(BaseModel):
    order_number: str
    status: str
    carrier: str | None
    service: str | None
    warehouse: str | None
    tracking_reference: str | None
    label_url: str | None
    allocation: AllocationResult


class ParcelOut(BaseModel):
    sequence: int
    weight_kg: str
    barcode: str
    carrier_barcode: str | None


class EventOut(BaseModel):
    stage: str
    detail: dict[str, object]
    created_at: datetime


class ConsignmentDetailOut(ConsignmentOut):
    recipient_name: str
    parcels: list[ParcelOut]
    events: list[EventOut]


def _label_url(consignment: Consignment) -> str | None:
    if consignment.status not in LABELLED_STATUSES:
        return None
    return f"/api/consignments/{consignment.order_number}/label.pdf"


@router.post("", status_code=201)
def create_consignment_endpoint(
    payload: ConsignmentIn,
    session: SessionDep,
    store: LabelStoreDep,
    http_client: HttpClientDep,
    uploaders: UploaderDep,
) -> ConsignmentOut:
    # The duplicate-order 409 is checked before the force_service 403, preserving
    # the order the two gates ran in when this lived inline; the domain re-checks
    # for the atomic race, but the ordering is the edge's contract.
    if consignments_domain.order_exists(session, payload.order_number):
        raise HTTPException(409, "a consignment already exists for this order")
    # force_service is a testing-tools capability, gated at the edge; the domain
    # trusts an already-authorised request (ADR 0002: policy stays out of the
    # shared core, edges apply their own).
    if payload.force_service is not None and not get_settings().testing_tools_enabled:
        raise HTTPException(
            403, "force_service requires testing tools, which are disabled here"
        )
    request = ConsignmentRequest(
        order_number=payload.order_number,
        recipient_name=payload.recipient_name,
        address_lines=payload.address_lines,
        postcode=payload.postcode,
        destination_country=payload.destination_country,
        proposition=payload.proposition,
        parcel_weights=[parcel.weight_kg for parcel in payload.parcels],
        warehouse=payload.warehouse,
        force_service=payload.force_service,
    )
    try:
        created = create_consignment(session, request, store, http_client, uploaders)
    except ConsignmentError as error:
        raise HTTPException(error.status, error.detail) from error
    consignment = created.consignment
    return ConsignmentOut(
        order_number=consignment.order_number,
        status=consignment.status,
        carrier=consignment.carrier,
        service=consignment.service,
        warehouse=consignment.warehouse,
        tracking_reference=consignment.tracking_reference,
        label_url=_label_url(consignment),
        allocation=created.allocation,
    )


def _get_consignment(session: Session, order_number: str) -> Consignment:
    consignment = session.execute(
        select(Consignment).where(Consignment.order_number == order_number)
    ).scalar_one_or_none()
    if consignment is None:
        raise HTTPException(404, "no consignment for this order")
    return consignment


@router.get("/{order_number}")
def consignment_detail(order_number: str, session: SessionDep) -> ConsignmentDetailOut:
    consignment = _get_consignment(session, order_number)
    events = (
        session.execute(
            select(OrderEvent)
            .where(OrderEvent.order_number == order_number)
            .order_by(OrderEvent.id)
        )
        .scalars()
        .all()
    )
    return ConsignmentDetailOut(
        order_number=consignment.order_number,
        status=consignment.status,
        carrier=consignment.carrier,
        service=consignment.service,
        warehouse=consignment.warehouse,
        tracking_reference=consignment.tracking_reference,
        label_url=_label_url(consignment),
        allocation=AllocationResult.model_validate(consignment.allocation),
        recipient_name=consignment.recipient_name,
        parcels=[
            ParcelOut(
                sequence=p.sequence,
                weight_kg=p.weight_kg,
                barcode=p.barcode,
                carrier_barcode=p.carrier_barcode,
            )
            for p in consignment.parcels
        ],
        events=[
            EventOut(stage=e.stage, detail=e.detail, created_at=e.created_at)
            for e in events
        ],
    )


@router.get("/{order_number}/label.pdf")
def consignment_label(
    order_number: str, session: SessionDep, store: LabelStoreDep
) -> Response:
    _get_consignment(session, order_number)
    pdf = store.load(order_number)
    if pdf is None:
        raise HTTPException(404, "no label for this order")
    return Response(content=pdf, media_type="application/pdf")
