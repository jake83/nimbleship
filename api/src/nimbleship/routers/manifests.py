"""Dispatch confirmations and Manifests. The WMS's "scan-out" (trailer
doors close) arrives here as a dispatch confirmation; the Manifests it
creates are sent to carriers asynchronously, so the endpoint answers as
soon as the consignments are marked and the send jobs are enqueued - in
one transaction (ADR 0004)."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.db import get_session
from nimbleship.domain.manifests import create_manifests, manifest_consignments
from nimbleship.models import Consignment, Manifest
from nimbleship.queue import defer_manifest_send

router = APIRouter(tags=["manifests"])

SessionDep = Annotated[Session, Depends(get_session)]


class DispatchConfirmationIn(BaseModel):
    order_numbers: list[str] = Field(min_length=1)


class ManifestOut(BaseModel):
    id: int
    carrier: str
    warehouse: str | None
    status: str
    attempts: int
    last_error: str | None
    created_at: datetime
    sent_at: datetime | None
    order_numbers: list[str]


class DispatchConfirmationOut(BaseModel):
    dispatched: list[str]
    manifests: list[ManifestOut]


def _manifest_out(session: Session, manifest: Manifest) -> ManifestOut:
    return ManifestOut(
        id=manifest.id,
        carrier=manifest.carrier,
        warehouse=manifest.warehouse,
        status=manifest.status,
        attempts=manifest.attempts,
        last_error=manifest.last_error,
        created_at=manifest.created_at,
        sent_at=manifest.sent_at,
        order_numbers=[
            consignment.order_number
            for consignment in manifest_consignments(session, manifest)
        ],
    )


@router.post("/dispatch-confirmations", status_code=201)
def confirm_dispatch(
    payload: DispatchConfirmationIn, session: SessionDep
) -> DispatchConfirmationOut:
    """The confirmation is transactional: every named consignment must
    exist and be dispatchable, or nothing is dispatched - a partial
    scan-out silently splitting into shipped-and-not would be exactly the
    ambiguity the Manifest concept exists to remove."""
    duplicates = sorted(
        {
            number
            for number in payload.order_numbers
            if payload.order_numbers.count(number) > 1
        }
    )
    if duplicates:
        raise HTTPException(
            422, f"order numbers repeat in the confirmation: {', '.join(duplicates)}"
        )
    rows = (
        session.execute(
            select(Consignment).where(
                Consignment.order_number.in_(payload.order_numbers)
            )
        )
        .scalars()
        .all()
    )
    by_number = {row.order_number: row for row in rows}
    unknown = [n for n in payload.order_numbers if n not in by_number]
    if unknown:
        raise HTTPException(
            422, f"no consignment for order numbers: {', '.join(unknown)}"
        )
    undispatchable = [
        f"{n} ({by_number[n].status})"
        for n in payload.order_numbers
        if by_number[n].status != "allocated"
    ]
    if undispatchable:
        raise HTTPException(
            409,
            "only allocated consignments can be dispatched: "
            + ", ".join(undispatchable),
        )

    consignments = [by_number[n] for n in payload.order_numbers]
    manifests = create_manifests(session, consignments)
    for manifest in manifests:
        defer_manifest_send(session, manifest.id)

    return DispatchConfirmationOut(
        dispatched=[c.order_number for c in consignments],
        manifests=[_manifest_out(session, m) for m in manifests],
    )


@router.get("/manifests")
def list_manifests(session: SessionDep) -> list[ManifestOut]:
    manifests = (
        session.execute(select(Manifest).order_by(Manifest.id.desc())).scalars().all()
    )
    return [_manifest_out(session, manifest) for manifest in manifests]


@router.get("/manifests/{manifest_id}")
def manifest_detail(manifest_id: int, session: SessionDep) -> ManifestOut:
    manifest = session.get(Manifest, manifest_id)
    if manifest is None:
        raise HTTPException(404, "no such manifest")
    return _manifest_out(session, manifest)
