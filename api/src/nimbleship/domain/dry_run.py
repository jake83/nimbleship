"""Dry-run a candidate rulebook against historical orders (ADR 0003): replay the
recorded consignments through it and report what would change - possible because
allocate() is pure. Shared by the rulebook route (an already-saved version) and the
rules builder (an unsaved working copy, ADR 0017)."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nimbleship.domain.allocation import Rulebook, Shipment, allocate
from nimbleship.domain.geography import resolve_shipping_areas
from nimbleship.models import Consignment

DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class DryRunResult:
    order_number: str
    current_service: str | None
    draft_service: str | None
    changed: bool


@dataclass(frozen=True)
class DryRunReport:
    total: int
    changed: int
    results: list[DryRunResult]


def _shipment_from(session: Session, consignment: Consignment) -> Shipment:
    """Rebuild the dispatch-time facts. Areas are re-resolved from the stored
    postcode: replaying without them would evaluate area checks optimistically and
    misreport outcomes the live run rejected. Resolution uses today's geography
    tables by design - a replay answers "what would this version do now", so a
    divergence can reflect area-definition drift as well as rulebook drift."""
    return Shipment(
        order_number=consignment.order_number,
        destination_country=consignment.destination_country,
        total_weight_kg=sum(
            (Decimal(p.weight_kg) for p in consignment.parcels), Decimal("0")
        ),
        parcel_count=len(consignment.parcels),
        proposition=consignment.proposition,
        accepted_service_groups=consignment.accepted_service_groups,
        max_dimension_cm=(
            Decimal(consignment.max_dimension_cm)
            if consignment.max_dimension_cm is not None
            else None
        ),
        max_girth_cm=(
            Decimal(consignment.max_girth_cm)
            if consignment.max_girth_cm is not None
            else None
        ),
        shipping_areas=resolve_shipping_areas(
            session, consignment.postcode, consignment.destination_country
        ),
        warehouse=consignment.warehouse,
    )


def dry_run_rulebook(
    session: Session,
    rulebook: Rulebook,
    order_numbers: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> DryRunReport:
    """Replay historical consignments (a named set, else the most recent `limit`)
    through `rulebook` and report which orders would change service."""
    query = (
        select(Consignment)
        .options(selectinload(Consignment.parcels))
        .order_by(Consignment.id.desc())
    )
    if order_numbers is not None:
        query = query.where(Consignment.order_number.in_(order_numbers))
    else:
        query = query.limit(limit)
    consignments = list(session.execute(query).scalars())

    results: list[DryRunResult] = []
    for consignment in consignments:
        outcome = allocate(rulebook, _shipment_from(session, consignment))
        draft_service = outcome.selected.code if outcome.selected is not None else None
        results.append(
            DryRunResult(
                order_number=consignment.order_number,
                current_service=consignment.service,
                draft_service=draft_service,
                changed=draft_service != consignment.service,
            )
        )
    return DryRunReport(
        total=len(results),
        changed=sum(1 for r in results if r.changed),
        results=results,
    )
