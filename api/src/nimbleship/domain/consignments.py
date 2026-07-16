"""The create-consignment operation as a domain service (ADR 0002): allocate,
book the carrier, produce the label, record the timeline. Both protocol edges
call this - the JSON API directly, the Legacy Interface at paperwork - so the
orchestration lives here, not in either edge. Failures raise ConsignmentError
carrying the status and message each edge maps to its own error shape."""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nimbleship.carriers.dropout import LabelRequest, LabelSender, render_labels
from nimbleship.domain.allocation import (
    AllocationResult,
    Shipment,
    allocate,
    selection_cost,
)
from nimbleship.domain.barcodes import parcel_barcodes
from nimbleship.domain.carrier_definition import AllocationSpec, CarrierDefinition
from nimbleship.domain.definitions import active_definition, carrier_config
from nimbleship.domain.facts import shipment_facts, warehouse_facts
from nimbleship.domain.geography import resolve_shipping_areas
from nimbleship.domain.rulebook import active_rulebook
from nimbleship.engine.execute import CarrierCallError, StepRecord, execute_operation
from nimbleship.engine.plugins.number_range import (
    RangeExhausted,
    allocate_number,
    assemble_sscc,
    sscc_sequence_name,
    sscc_wrap_after,
)
from nimbleship.labels.store import LabelStore
from nimbleship.models import CarrierTraffic, Consignment, OrderEvent, Parcel, Warehouse
from nimbleship.uploaders import FileUploader


class ConsignmentError(Exception):
    """A create-consignment failure with the HTTP-style status and message each
    edge translates: the JSON API to an HTTPException, the Legacy Interface to a
    SOAP fault. The status is the JSON API's contract, kept exactly as it was
    when this orchestration lived in that router."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass
class ConsignmentRequest:
    order_number: str
    recipient_name: str
    address_lines: list[str]
    postcode: str
    destination_country: str
    proposition: str | None
    parcel_weights: list[Decimal]
    warehouse: str | None
    # Testing tools only: pins the allocation to one service. The edge is
    # responsible for gating this (the JSON API's 403); the domain trusts it.
    force_service: str | None = None


@dataclass
class CreatedConsignment:
    consignment: Consignment
    allocation: AllocationResult


def order_exists(session: Session, order_number: str) -> bool:
    row = session.execute(
        select(Consignment.id).where(Consignment.order_number == order_number)
    ).scalar_one_or_none()
    return row is not None


def _resolve_warehouse(session: Session, code: str | None) -> Warehouse | None:
    """Look up the named Warehouse; an unknown code is a caller error, not a
    fact to store optimistically - fail before anything is written."""
    if code is None:
        return None
    warehouse = session.execute(
        select(Warehouse).where(Warehouse.code == code)
    ).scalar_one_or_none()
    if warehouse is None:
        raise ConsignmentError(422, "unknown warehouse code")
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


def _base64_pdf_label(
    outputs: dict[str, object], from_extract: str, carrier: str
) -> bytes:
    """Decode the base64 PDF a carrier returned in its book response. The label
    is the exact document the carrier produced, so a missing, non-string,
    non-base64, or non-PDF value is a failed booking (502), never a silent bad
    label the warehouse would print."""
    raw = outputs.get(from_extract)
    if not isinstance(raw, str) or not raw:
        raise ConsignmentError(
            502,
            f"carrier '{carrier}' book response has no base64 label at "
            f"'{from_extract}'",
        )
    try:
        # Strip whitespace first: a carrier's JSON may line-wrap the base64
        # (MIME-style) or leave a trailing newline, which validate=True would
        # otherwise reject as a false booking failure on a valid label.
        pdf = base64.b64decode("".join(raw.split()), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConsignmentError(
            502, f"carrier '{carrier}' returned an invalid base64 label: {error}"
        ) from error
    if not pdf.startswith(b"%PDF"):
        raise ConsignmentError(502, f"carrier '{carrier}' base64 label is not a PDF")
    return pdf


def _config_key(source: str) -> str:
    # An allocate prefix is a schema-enforced config.* source; the bare key
    # follows the first dot.
    return source.split(".", 1)[1]


def _mint_parcel_allocations(
    session: Session,
    consignment: Consignment,
    specs: list[AllocationSpec],
    config: dict[str, object],
) -> None:
    """Mint each parcel's SSCC before the book call and store it on
    parcel.carrier_barcode, where shipment_facts exposes it to the book
    mapping. A client-assigned code must be minted once, never echoed from a
    response.

    Minting commits in its own transaction (like the traffic rows): the
    allocation lock releases before the carrier call, and a halt-range number
    is spent the instant it is issued, so a crash never reissues a code that
    may have reached the carrier - at the cost of wasting a failed booking's
    numbers. All parcels mint or none do: a range exhausted partway rolls back
    and fails the booking loudly."""
    carrier = consignment.carrier or ""
    with Session(session.get_bind()) as mint_session:
        for spec in specs:
            prefix = config.get(_config_key(spec.prefix))
            if not isinstance(prefix, str):
                raise ConsignmentError(
                    500,
                    f"carrier '{carrier}' allocate prefix '{spec.prefix}' is not "
                    "configured; provision it before dispatch",
                )
            try:
                # The serial fills whatever the prefix leaves of the 17-digit
                # body; wrap_after is the last serial that still fits.
                wrap_after = sscc_wrap_after(prefix)
            except ValueError as error:
                raise ConsignmentError(
                    500, f"carrier '{carrier}' SSCC prefix is invalid: {error}"
                ) from error
            for parcel in consignment.parcels:
                try:
                    serial = allocate_number(
                        mint_session,
                        carrier,
                        sscc_sequence_name(prefix),
                        wrap_after=wrap_after,
                        policy=spec.policy,
                    )
                except RangeExhausted as error:
                    raise ConsignmentError(
                        503,
                        f"carrier '{carrier}' SSCC range exhausted; provision a "
                        f"new prefix before dispatch: {error}",
                    ) from error
                parcel.carrier_barcode = assemble_sscc(prefix, int(serial))
        mint_session.commit()


def _book_with_carrier(
    session: Session,
    definition: CarrierDefinition,
    consignment: Consignment,
    warehouse: Warehouse | None,
    http_client: httpx.Client,
    uploaders: Mapping[str, FileUploader],
) -> dict[str, object]:
    """Execute the book operation's http steps, recording every step as carrier
    traffic (ADR 0009's golden corpus grows from real calls). On success the
    extracted tracking reference and carrier barcodes land on the consignment;
    on failure a booking_failed event is committed before the 502 - never a
    silent success.

    Carrier contact always commits traffic: every step's traffic row is
    committed in its own transaction the moment the call returns, so no later
    failure of the request - a duplicate-order 409 losing the unique-constraint
    race, a label error, anything - can discard the audit trail of a call that
    really reached the carrier (refuter, PR #30)."""
    # Facts are gathered without autoflush: the request session must not hold an
    # open write transaction (its speculative consignment insert) while the
    # carrier is on the line - the traffic commits below run on their own
    # connections and must never queue behind this request's locks, and a racing
    # duplicate submission must not block on this request's uncommitted row.
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
        result = execute_operation(
            definition, "book", facts, http_client, record, uploaders
        )
    except CarrierCallError as error:
        consignment.status = "booking_failed"
        session.add(
            OrderEvent(
                order_number=consignment.order_number,
                stage="booking_failed",
                detail={"carrier": consignment.carrier, "error": str(error)},
            )
        )
        # Commit explicitly: raising unwinds the caller before its normal commit,
        # and a failure's timeline must survive the 502 (the traffic already
        # committed in its own transaction above).
        try:
            session.flush()
            session.commit()
        except IntegrityError as dup:
            # A duplicate won the row while this one was on the carrier's line:
            # surface the 409 (like the success path), not a 500 - the order is
            # the winner's.
            raise ConsignmentError(
                409, "a consignment already exists for this order"
            ) from dup
        raise ConsignmentError(502, str(error)) from error

    # The extraction names "tracking_reference" and "barcodes" are the contract
    # between a book operation and this flow: a definition must extract under
    # exactly these names for the values to reach the consignment (see
    # api/examples/furdeco.definition.json).
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
        # Carrier barcodes pair with parcels positionally, like the labels they
        # arrive on; the full list is kept on the event so a count mismatch loses
        # nothing. A code minted before the call (an SSCC) is never overwritten by
        # the response: the minted code is the one applied and sent.
        for parcel, barcode in zip(consignment.parcels, barcodes, strict=False):
            if parcel.carrier_barcode is None:
                parcel.carrier_barcode = str(barcode)
        detail["barcodes"] = [str(b) for b in barcodes]
    session.add(
        OrderEvent(order_number=consignment.order_number, stage="booked", detail=detail)
    )
    return result.outputs


def create_consignment(
    session: Session,
    request: ConsignmentRequest,
    store: LabelStore,
    http_client: httpx.Client,
    uploaders: Mapping[str, FileUploader],
) -> CreatedConsignment:
    if order_exists(session, request.order_number):
        raise ConsignmentError(409, "a consignment already exists for this order")
    warehouse = _resolve_warehouse(session, request.warehouse)

    rulebook = active_rulebook(session)
    total_weight = sum(request.parcel_weights, Decimal("0"))
    # Area facts are resolved before evaluation so allocate() stays pure
    # (ADR 0008 addendum): facts in, verdict and trace out.
    shipping_areas = resolve_shipping_areas(
        session, request.postcode, request.destination_country
    )
    shipment = Shipment(
        order_number=request.order_number,
        destination_country=request.destination_country,
        total_weight_kg=total_weight,
        parcel_count=len(request.parcel_weights),
        proposition=request.proposition,
        shipping_areas=shipping_areas,
        warehouse=request.warehouse,
    )
    result = allocate(rulebook, shipment)
    if request.force_service is not None:
        forced = next(
            (s for s in rulebook.services if s.code == request.force_service), None
        )
        if forced is None:
            raise ConsignmentError(
                422, "force_service names no service in the rulebook"
            )
        # The genuine evaluation trace is kept; only the selection is overridden,
        # so the audit trail shows both what would have happened and that it was
        # forced. The forced cost comes from the selection policy's own helper -
        # one definition of "the cost", never a drifting copy.
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
        # A service selectable by the rulebook but whose carrier has no published
        # Carrier Definition is a configuration error - loud, never a silent skip
        # or a mystery failure later at booking.
        raise ConsignmentError(
            500,
            f"no published carrier definition for '{selected.carrier}': "
            "publish one before its services can dispatch",
        )

    consignment = Consignment(
        order_number=request.order_number,
        recipient_name=request.recipient_name,
        address_lines=request.address_lines,
        postcode=request.postcode,
        destination_country=request.destination_country,
        proposition=request.proposition,
        status="allocated" if selected else "rejected",
        carrier=selected.carrier if selected else None,
        service=selected.code if selected else None,
        warehouse=request.warehouse,
        allocation=result.model_dump(mode="json"),
    )
    barcodes = parcel_barcodes(request.order_number, len(request.parcel_weights))
    consignment.parcels = [
        Parcel(sequence=i, weight_kg=str(weight), barcode=barcode)
        for i, (weight, barcode) in enumerate(
            zip(request.parcel_weights, barcodes, strict=True), start=1
        )
    ]
    session.add(consignment)

    if selected is None:
        session.add(
            OrderEvent(
                order_number=request.order_number,
                stage="rejected",
                detail={"reason": result.reason},
            )
        )
    else:
        session.add(
            OrderEvent(
                order_number=request.order_number,
                stage="allocated",
                detail={
                    "carrier": selected.carrier,
                    "service": selected.code,
                    # The cost selection compared (banded when configured), not
                    # the flat `selected.cost` fallback field. Absent cost (a
                    # forced service with no matching band) is JSON null - the
                    # audit trail never carries a stringified None (refuter,
                    # PR #25).
                    "cost": str(result.selected_cost)
                    if result.selected_cost is not None
                    else None,
                    "rulebook_version": rulebook.version,
                    "forced": request.force_service is not None,
                },
            )
        )
        assert definition is not None
        book = definition.operations.get("book")
        if book is None:
            raise ConsignmentError(
                500,
                f"carrier '{selected.carrier}' has no book operation in its "
                "published definition; it cannot dispatch consignments",
            )
        label_spec = book.label
        # The label source is checked before any carrier call: an unsupported
        # source must fail before a booking exists on the carrier's side.
        if label_spec is None or label_spec.source not in (
            "local_render",
            "base64_pdf",
        ):
            raise ConsignmentError(
                500,
                f"carrier '{selected.carrier}' label source is unsupported; only "
                "local_render and base64_pdf are supported so far",
            )
        if book.allocate:
            # Mint the parcels' SSCCs before the carrier call; the declaration
            # names what to mint, so no carrier name is hardcoded. no_autoflush
            # keeps the speculative consignment insert from flushing here and
            # holding a write transaction open across the carrier call (the
            # hazard _book_with_carrier guards the same way).
            with session.no_autoflush:
                config = carrier_config(session, selected.carrier or "")
            _mint_parcel_allocations(session, consignment, book.allocate, config)
        outputs: dict[str, object] = {}
        if book.steps:
            outputs = _book_with_carrier(
                session, definition, consignment, warehouse, http_client, uploaders
            )
        if label_spec.source == "base64_pdf":
            # The carrier returned the label as a base64 PDF in its book response;
            # the extraction the label names carries it.
            assert label_spec.from_extract is not None  # schema-guaranteed
            try:
                pdf = _base64_pdf_label(
                    outputs, label_spec.from_extract, selected.carrier or ""
                )
            except ConsignmentError:
                if book.steps:
                    # The carrier call already created the shipment, so a label we
                    # cannot decode must not discard the booking: the shipment
                    # would live at the carrier with no local record, and a retry
                    # would double-book. Persist it as failed (a retry then 409s)
                    # before re-raising.
                    consignment.status = "label_failed"
                    session.add(
                        OrderEvent(
                            order_number=request.order_number,
                            stage="label_failed",
                            detail={"carrier": selected.carrier},
                        )
                    )
                    try:
                        session.flush()
                        session.commit()
                    except IntegrityError as dup:
                        # A duplicate won the row while this one was on the
                        # carrier's line: surface the 409 (like the success path),
                        # not a 500 - the order is the winner's.
                        raise ConsignmentError(
                            409, "a consignment already exists for this order"
                        ) from dup
                raise
        else:
            pdf = render_labels(
                LabelRequest(
                    order_number=request.order_number,
                    recipient_name=request.recipient_name,
                    address_lines=request.address_lines,
                    postcode=request.postcode,
                    country=request.destination_country,
                    parcel_count=len(request.parcel_weights),
                    sender=_label_sender(warehouse),
                )
            )
        store.save(request.order_number, pdf)
        session.add(
            OrderEvent(
                order_number=request.order_number,
                stage="label_created",
                detail={"pages": len(request.parcel_weights)},
            )
        )

    try:
        session.flush()
    except IntegrityError as error:
        # Losing a duplicate race: the unique constraint is the last line of
        # defence behind the order_exists pre-check (refuter finding, PR #6).
        raise ConsignmentError(
            409, "a consignment already exists for this order"
        ) from error
    return CreatedConsignment(consignment=consignment, allocation=result)
