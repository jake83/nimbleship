from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nimbleship.carriers.dropout import LabelRequest, LabelSender, render_labels
from nimbleship.db import get_session
from nimbleship.domain.allocation import AllocationResult, Shipment, allocate
from nimbleship.domain.barcodes import parcel_barcodes
from nimbleship.domain.rulebook import active_rulebook
from nimbleship.labels.store import LabelStore, get_label_store
from nimbleship.models import Consignment, OrderEvent, Parcel, Warehouse

router = APIRouter(prefix="/consignments", tags=["consignments"])

SessionDep = Annotated[Session, Depends(get_session)]
LabelStoreDep = Annotated[LabelStore, Depends(get_label_store)]


class ParcelIn(BaseModel):
    weight_kg: Decimal = Field(gt=0)


class ConsignmentIn(BaseModel):
    # ASCII only: order numbers become Code 128 barcodes, whose encoder
    # rejects anything outside Latin-1 (refuter finding, PR #6).
    order_number: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    recipient_name: str
    address_lines: list[str]
    postcode: str
    destination_country: str = Field(min_length=2, max_length=3)
    parcels: list[ParcelIn] = Field(min_length=1)
    # The Warehouse code the consignment dispatches from (CONTEXT.md:
    # Warehouse - a logical dispatch identity); it supplies the label's
    # sender details.
    warehouse: str | None = Field(default=None, max_length=64)


class ConsignmentOut(BaseModel):
    order_number: str
    status: str
    carrier: str | None
    service: str | None
    warehouse: str | None
    label_url: str | None
    allocation: AllocationResult


class ParcelOut(BaseModel):
    sequence: int
    weight_kg: str
    barcode: str


class EventOut(BaseModel):
    stage: str
    detail: dict[str, object]
    created_at: datetime


class ConsignmentDetailOut(ConsignmentOut):
    recipient_name: str
    parcels: list[ParcelOut]
    events: list[EventOut]


def _label_url(consignment: Consignment) -> str | None:
    if consignment.status != "allocated":
        return None
    return f"/api/consignments/{consignment.order_number}/label.pdf"


def _order_exists(session: Session, order_number: str) -> bool:
    row = session.execute(
        select(Consignment.id).where(Consignment.order_number == order_number)
    ).scalar_one_or_none()
    return row is not None


def _resolve_warehouse(session: Session, code: str | None) -> Warehouse | None:
    """Look up the named Warehouse; an unknown code is a caller error, not
    a fact to store optimistically - fail before anything is written."""
    if code is None:
        return None
    warehouse = session.execute(
        select(Warehouse).where(Warehouse.code == code)
    ).scalar_one_or_none()
    if warehouse is None:
        raise HTTPException(422, "unknown warehouse code")
    return warehouse


def _label_sender(warehouse: Warehouse | None) -> LabelSender | None:
    if warehouse is None:
        return None
    return LabelSender(
        name=warehouse.company_name or warehouse.name,
        address_lines=warehouse.address_lines,
        postcode=warehouse.postcode,
        country=warehouse.country,
    )


@router.post("", status_code=201)
def create_consignment(
    payload: ConsignmentIn, session: SessionDep, store: LabelStoreDep
) -> ConsignmentOut:
    if _order_exists(session, payload.order_number):
        raise HTTPException(409, "a consignment already exists for this order")
    warehouse = _resolve_warehouse(session, payload.warehouse)

    rulebook = active_rulebook(session)
    total_weight = sum((p.weight_kg for p in payload.parcels), Decimal("0"))
    result = allocate(
        rulebook,
        Shipment(
            order_number=payload.order_number,
            destination_country=payload.destination_country,
            total_weight_kg=total_weight,
            parcel_count=len(payload.parcels),
        ),
    )

    selected = result.selected
    consignment = Consignment(
        order_number=payload.order_number,
        recipient_name=payload.recipient_name,
        address_lines=payload.address_lines,
        postcode=payload.postcode,
        destination_country=payload.destination_country,
        status="allocated" if selected else "rejected",
        carrier=selected.carrier if selected else None,
        service=selected.code if selected else None,
        warehouse=payload.warehouse,
        allocation=result.model_dump(mode="json"),
    )
    barcodes = parcel_barcodes(payload.order_number, len(payload.parcels))
    consignment.parcels = [
        Parcel(sequence=i, weight_kg=str(p.weight_kg), barcode=barcode)
        for i, (p, barcode) in enumerate(
            zip(payload.parcels, barcodes, strict=True), start=1
        )
    ]
    session.add(consignment)

    if selected is None:
        session.add(
            OrderEvent(
                order_number=payload.order_number,
                stage="rejected",
                detail={"reason": result.reason},
            )
        )
    else:
        session.add(
            OrderEvent(
                order_number=payload.order_number,
                stage="allocated",
                detail={
                    "carrier": selected.carrier,
                    "service": selected.code,
                    "cost": str(selected.cost),
                    "rulebook_version": rulebook.version,
                },
            )
        )
        pdf = render_labels(
            LabelRequest(
                order_number=payload.order_number,
                recipient_name=payload.recipient_name,
                address_lines=payload.address_lines,
                postcode=payload.postcode,
                country=payload.destination_country,
                parcel_count=len(payload.parcels),
                sender=_label_sender(warehouse),
            )
        )
        store.save(payload.order_number, pdf)
        session.add(
            OrderEvent(
                order_number=payload.order_number,
                stage="label_created",
                detail={"pages": len(payload.parcels)},
            )
        )

    try:
        session.flush()
    except IntegrityError as error:
        # Losing a duplicate race: the unique constraint is the last line of
        # defence behind the _order_exists pre-check (refuter finding, PR #6).
        raise HTTPException(
            409, "a consignment already exists for this order"
        ) from error
    return ConsignmentOut(
        order_number=consignment.order_number,
        status=consignment.status,
        carrier=consignment.carrier,
        service=consignment.service,
        warehouse=consignment.warehouse,
        label_url=_label_url(consignment),
        allocation=result,
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
        label_url=_label_url(consignment),
        allocation=AllocationResult.model_validate(consignment.allocation),
        recipient_name=consignment.recipient_name,
        parcels=[
            ParcelOut(sequence=p.sequence, weight_kg=p.weight_kg, barcode=p.barcode)
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
