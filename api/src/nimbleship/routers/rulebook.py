from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nimbleship.db import get_session
from nimbleship.domain.allocation import (
    Rulebook,
    ServiceDeclaration,
    Shipment,
    allocate,
)
from nimbleship.domain.geography import resolve_shipping_areas
from nimbleship.domain.rulebook import (
    active_rulebook,
    create_draft,
    get_version,
    list_versions,
    publish,
    rulebook_for,
)
from nimbleship.models import Consignment, RulebookVersion

router = APIRouter(prefix="/rulebook", tags=["rulebook"])

SessionDep = Annotated[Session, Depends(get_session)]

DRY_RUN_DEFAULT_LIMIT = 50


class DraftIn(BaseModel):
    services: list[ServiceDeclaration]
    # Placeholder identity pending the auth story; bounded to the column.
    author: str = Field(default="api", max_length=64)


class VersionOut(BaseModel):
    version: int
    status: str
    author: str


class VersionDetailOut(VersionOut):
    created_at: datetime


class VersionContentOut(VersionDetailOut):
    services: list[ServiceDeclaration]


class DryRunIn(BaseModel):
    # Bounded like limit: naming orders must not bypass the replay cap.
    order_numbers: list[str] | None = Field(default=None, max_length=500)
    limit: int = Field(default=DRY_RUN_DEFAULT_LIMIT, ge=1, le=500)


class DryRunResultOut(BaseModel):
    order_number: str
    current_service: str | None
    draft_service: str | None
    changed: bool


class DryRunOut(BaseModel):
    rulebook_version: int
    total: int
    changed: int
    results: list[DryRunResultOut]


@router.get("/active")
def active(session: SessionDep) -> Rulebook:
    return active_rulebook(session)


@router.get("/versions")
def versions(session: SessionDep) -> list[VersionDetailOut]:
    return [
        VersionDetailOut(
            version=row.version,
            status=row.status,
            author=row.author,
            created_at=row.created_at,
        )
        for row in list_versions(session)
    ]


@router.post("/drafts", status_code=201)
def create_draft_version(payload: DraftIn, session: SessionDep) -> VersionOut:
    try:
        row = create_draft(session, payload.services, payload.author)
    except ValueError as error:
        # Covers pydantic's ValidationError (a ValueError subclass) and the
        # catalogue check in create_draft alike: both are authoring errors.
        raise HTTPException(422, str(error)) from error
    return VersionOut(version=row.version, status=row.status, author=row.author)


def _get_version_or_404(session: Session, version: int) -> RulebookVersion:
    row = get_version(session, version)
    if row is None:
        raise HTTPException(404, "no such rulebook version")
    return row


@router.get("/versions/{version}")
def version_content(version: int, session: SessionDep) -> VersionContentOut:
    """One version with its full service content - what the UI diffs,
    edits from, and inspects; the list endpoint stays metadata-only."""
    row = _get_version_or_404(session, version)
    return VersionContentOut(
        version=row.version,
        status=row.status,
        author=row.author,
        created_at=row.created_at,
        services=rulebook_for(row).services,
    )


@router.post("/versions/{version}/publish")
def publish_version(version: int, session: SessionDep) -> VersionOut:
    row = _get_version_or_404(session, version)
    try:
        publish(session, row)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return VersionOut(version=row.version, status=row.status, author=row.author)


def _shipment_from(session: Session, consignment: Consignment) -> Shipment:
    """Rebuild the dispatch-time facts. Areas are re-resolved from the
    stored postcode: replaying without them would evaluate area checks
    optimistically and misreport outcomes the live run rejected.

    Resolution uses TODAY's geography tables by design - a replay answers
    "what would this version do now", so a divergence can reflect area
    definition drift as well as rulebook drift."""
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
        shipping_areas=resolve_shipping_areas(
            session, consignment.postcode, consignment.destination_country
        ),
        warehouse=consignment.warehouse,
    )


@router.post("/versions/{version}/dry-run")
def dry_run(version: int, payload: DryRunIn, session: SessionDep) -> DryRunOut:
    """Replay historical consignments through a rulebook version and report
    what would change - the ADR 0003 'test' step, possible because
    allocate() is a pure function."""
    row = _get_version_or_404(session, version)
    rulebook = rulebook_for(row)

    query = (
        select(Consignment)
        .options(selectinload(Consignment.parcels))
        .order_by(Consignment.id.desc())
    )
    if payload.order_numbers is not None:
        query = query.where(Consignment.order_number.in_(payload.order_numbers))
    else:
        query = query.limit(payload.limit)
    consignments = list(session.execute(query).scalars())

    results = []
    for consignment in consignments:
        outcome = allocate(rulebook, _shipment_from(session, consignment))
        draft_service = outcome.selected.code if outcome.selected else None
        results.append(
            DryRunResultOut(
                order_number=consignment.order_number,
                current_service=consignment.service,
                draft_service=draft_service,
                changed=draft_service != consignment.service,
            )
        )

    return DryRunOut(
        rulebook_version=rulebook.version,
        total=len(results),
        changed=sum(1 for r in results if r.changed),
        results=results,
    )
