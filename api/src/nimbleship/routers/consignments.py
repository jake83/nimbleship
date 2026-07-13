from datetime import datetime
from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nimbleship.carriers.dropout import LabelRequest, LabelSender, render_labels
from nimbleship.config import get_settings
from nimbleship.db import get_session
from nimbleship.domain.allocation import (
    AllocationResult,
    Shipment,
    allocate,
    selection_cost,
)
from nimbleship.domain.barcodes import parcel_barcodes
from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.domain.definitions import active_definition, carrier_config
from nimbleship.domain.facts import shipment_facts, warehouse_facts
from nimbleship.domain.geography import resolve_shipping_areas
from nimbleship.domain.rulebook import active_rulebook
from nimbleship.engine.execute import (
    CarrierCallError,
    StepRecord,
    execute_operation,
)
from nimbleship.http_client import get_http_client
from nimbleship.labels.store import LabelStore, get_label_store
from nimbleship.models import CarrierTraffic, Consignment, OrderEvent, Parcel, Warehouse

router = APIRouter(prefix="/consignments", tags=["consignments"])

SessionDep = Annotated[Session, Depends(get_session)]
LabelStoreDep = Annotated[LabelStore, Depends(get_label_store)]
HttpClientDep = Annotated[httpx.Client, Depends(get_http_client)]


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


def _book_with_carrier(
    session: Session,
    definition: CarrierDefinition,
    consignment: Consignment,
    warehouse: Warehouse | None,
    http_client: httpx.Client,
) -> None:
    """Execute the book operation's http steps, recording every step as
    carrier traffic (ADR 0009's golden corpus grows from real calls). On
    success the extracted tracking reference and carrier barcodes land on
    the consignment; on failure a booking_failed event is committed before
    the 502 - never a silent success.

    Carrier contact always commits traffic: every step's traffic row is
    committed in its own transaction the moment the call returns, so no
    later failure of the request - a duplicate-order 409 losing the
    unique-constraint race, a label error, anything - can discard the
    audit trail of a call that really reached the carrier (refuter,
    PR #30)."""
    # Facts are gathered without autoflush: the request session must not
    # hold an open write transaction (its speculative consignment insert)
    # while the carrier is on the line - the traffic commits below run on
    # their own connections and must never queue behind this request's
    # locks, and a racing duplicate submission must not block on this
    # request's uncommitted row either.
    with session.no_autoflush:
        facts: dict[str, object] = {
            "shipment": shipment_facts(consignment),
            "config": carrier_config(session, consignment.carrier or ""),
        }
        if warehouse is not None:
            facts["warehouse"] = warehouse_facts(warehouse)

    def record(step_record: StepRecord) -> None:
        with Session(session.get_bind()) as traffic_session:
            traffic_session.add(
                CarrierTraffic(
                    carrier=consignment.carrier or "",
                    order_number=consignment.order_number,
                    step=step_record.step,
                    request=step_record.request.model_dump(mode="json"),
                    response_status=step_record.response_status,
                    response_body=step_record.response_body,
                )
            )
            traffic_session.commit()

    try:
        result = execute_operation(definition, "book", facts, http_client, record)
    except CarrierCallError as error:
        consignment.status = "booking_failed"
        session.add(
            OrderEvent(
                order_number=consignment.order_number,
                stage="booking_failed",
                detail={"carrier": consignment.carrier, "error": str(error)},
            )
        )
        session.flush()
        # Commit explicitly: raising unwinds the session dependency before
        # its normal commit, and a failure's timeline must survive the 502
        # (the traffic already committed in its own transaction above).
        session.commit()
        raise HTTPException(502, str(error)) from error

    # The extraction names "tracking_reference" and "barcodes" are the
    # contract between a book operation and this flow: a definition must
    # extract under exactly these names for the values to reach the
    # consignment (see api/examples/furdeco.definition.json).
    tracking = result.outputs.get("tracking_reference")
    if tracking is not None:
        consignment.tracking_reference = str(tracking)
    barcodes = result.outputs.get("barcodes")
    detail: dict[str, object] = {
        "carrier": consignment.carrier,
        "tracking_reference": consignment.tracking_reference,
        "steps": [
            {"step": r.step, "status": r.response_status, "success": r.success}
            for r in result.records
        ],
    }
    if isinstance(barcodes, list):
        # Carrier barcodes pair with parcels positionally, like the labels
        # they arrive on; the full list is kept on the event so a count
        # mismatch loses nothing.
        for parcel, barcode in zip(consignment.parcels, barcodes, strict=False):
            parcel.carrier_barcode = str(barcode)
        detail["barcodes"] = [str(b) for b in barcodes]
    session.add(
        OrderEvent(
            order_number=consignment.order_number,
            stage="booked",
            detail=detail,
        )
    )


@router.post("", status_code=201)
def create_consignment(
    payload: ConsignmentIn,
    session: SessionDep,
    store: LabelStoreDep,
    http_client: HttpClientDep,
) -> ConsignmentOut:
    if _order_exists(session, payload.order_number):
        raise HTTPException(409, "a consignment already exists for this order")
    if payload.force_service is not None and not get_settings().testing_tools_enabled:
        raise HTTPException(
            403, "force_service requires testing tools, which are disabled here"
        )
    warehouse = _resolve_warehouse(session, payload.warehouse)

    rulebook = active_rulebook(session)
    total_weight = sum((p.weight_kg for p in payload.parcels), Decimal("0"))
    # Area facts are resolved before evaluation so allocate() stays pure
    # (ADR 0008 addendum): facts in, verdict and trace out.
    shipping_areas = resolve_shipping_areas(
        session, payload.postcode, payload.destination_country
    )
    shipment = Shipment(
        order_number=payload.order_number,
        destination_country=payload.destination_country,
        total_weight_kg=total_weight,
        parcel_count=len(payload.parcels),
        proposition=payload.proposition,
        shipping_areas=shipping_areas,
        warehouse=payload.warehouse,
    )
    result = allocate(rulebook, shipment)
    if payload.force_service is not None:
        forced = next(
            (s for s in rulebook.services if s.code == payload.force_service), None
        )
        if forced is None:
            raise HTTPException(422, "force_service names no service in the rulebook")
        # The genuine evaluation trace is kept; only the selection is
        # overridden, so the audit trail shows both what would have
        # happened and that it was forced. The forced cost comes from the
        # selection policy's own helper - one definition of "the cost",
        # never a drifting copy.
        forced_cost = selection_cost(forced, shipment)
        result = result.model_copy(
            update={
                "selected": forced,
                "selected_cost": forced_cost,
                "reason": "forced by testing tools",
            }
        )

    selected = result.selected
    definition = (
        active_definition(session, selected.carrier) if selected is not None else None
    )
    if selected is not None and definition is None:
        # A service selectable by the rulebook but whose carrier has no
        # published Carrier Definition is a configuration error - loud,
        # never a silent skip or a mystery failure later at booking.
        raise HTTPException(
            500,
            f"no published carrier definition for '{selected.carrier}': "
            "publish one before its services can dispatch",
        )

    consignment = Consignment(
        order_number=payload.order_number,
        recipient_name=payload.recipient_name,
        address_lines=payload.address_lines,
        postcode=payload.postcode,
        destination_country=payload.destination_country,
        proposition=payload.proposition,
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
                    # The cost selection compared (banded when configured),
                    # not the flat `selected.cost` fallback field. Absent
                    # cost (a forced service with no matching band) is JSON
                    # null - the audit trail never carries a stringified
                    # None (refuter, PR #25).
                    "cost": str(result.selected_cost)
                    if result.selected_cost is not None
                    else None,
                    "rulebook_version": rulebook.version,
                    "forced": payload.force_service is not None,
                },
            )
        )
        assert definition is not None
        book = definition.operations.get("book")
        if book is None:
            raise HTTPException(
                500,
                f"carrier '{selected.carrier}' has no book operation in its "
                "published definition; it cannot dispatch consignments",
            )
        label_spec = book.label
        # The label spec is checked before any carrier call: an unsupported
        # label source must fail before a booking exists on the carrier's
        # side, not after.
        if label_spec is None or label_spec.source != "local_render":
            raise HTTPException(
                500,
                f"carrier '{selected.carrier}' does not local_render labels; "
                "only the local_render label source is supported so far",
            )
        if book.steps:
            _book_with_carrier(session, definition, consignment, warehouse, http_client)
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
        tracking_reference=consignment.tracking_reference,
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
