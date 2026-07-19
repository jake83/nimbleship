from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from nimbleship.db import get_session
from nimbleship.domain.allocation import Rulebook, ServiceDeclaration
from nimbleship.domain.dry_run import dry_run_rulebook
from nimbleship.domain.rulebook import (
    active_rulebook,
    create_draft,
    description_of,
    get_version,
    list_versions,
    publish,
    rulebook_for,
)
from nimbleship.models import RulebookVersion

router = APIRouter(prefix="/rulebook", tags=["rulebook"])

SessionDep = Annotated[Session, Depends(get_session)]

DRY_RUN_DEFAULT_LIMIT = 50


class DraftIn(BaseModel):
    # Bounded so a draft POST can't ask the server to validate an unbounded array;
    # a real rulebook is far under this.
    services: list[ServiceDeclaration] = Field(max_length=500)
    # Placeholder identity pending the auth story; bounded to the column.
    author: str = Field(default="api", max_length=64)
    # Optional rationale note (ADR 0017): why this version exists.
    description: str | None = Field(default=None, max_length=280)


class VersionOut(BaseModel):
    version: int
    status: str
    author: str
    description: str | None = None


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
            description=description_of(row),
            created_at=row.created_at,
        )
        for row in list_versions(session)
    ]


@router.post("/drafts", status_code=201)
def create_draft_version(payload: DraftIn, session: SessionDep) -> VersionOut:
    try:
        row = create_draft(
            session, payload.services, payload.author, payload.description
        )
    except ValueError as error:
        # Covers pydantic's ValidationError (a ValueError subclass) and the
        # catalogue check in create_draft alike: both are authoring errors.
        raise HTTPException(422, str(error)) from error
    return VersionOut(
        version=row.version,
        status=row.status,
        author=row.author,
        description=description_of(row),
    )


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
        description=description_of(row),
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
    return VersionOut(
        version=row.version,
        status=row.status,
        author=row.author,
        description=description_of(row),
    )


@router.post("/versions/{version}/dry-run")
def dry_run(version: int, payload: DryRunIn, session: SessionDep) -> DryRunOut:
    """Replay historical consignments through a rulebook version and report
    what would change - the ADR 0003 'test' step, possible because
    allocate() is a pure function."""
    row = _get_version_or_404(session, version)
    rulebook = rulebook_for(row)
    report = dry_run_rulebook(session, rulebook, payload.order_numbers, payload.limit)
    return DryRunOut(
        rulebook_version=rulebook.version,
        total=report.total,
        changed=report.changed,
        results=[
            DryRunResultOut(
                order_number=result.order_number,
                current_service=result.current_service,
                draft_service=result.draft_service,
                changed=result.changed,
            )
            for result in report.results
        ],
    )
