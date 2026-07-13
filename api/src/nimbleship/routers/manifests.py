"""Dispatch confirmations and Manifests. The WMS's "scan-out" (trailer
doors close) arrives here as a dispatch confirmation; the Manifests it
creates are sent to carriers asynchronously, so the endpoint answers as
soon as the consignments are marked and the send jobs are enqueued - in
one transaction (ADR 0004)."""

from collections import Counter
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.db import get_session
from nimbleship.domain.manifests import create_manifests, manifest_consignments
from nimbleship.models import Consignment, Manifest, ManifestConsignment
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


# Bound the listing: a manifest accrues per (carrier, warehouse) per
# dispatch day and is never pruned, so the endpoint an ops view polls must
# not grow unboundedly. Newest first; older manifests are fetched by id.
MANIFEST_LIST_LIMIT = 200


def _manifest_out(manifest: Manifest, order_numbers: list[str]) -> ManifestOut:
    return ManifestOut(
        id=manifest.id,
        carrier=manifest.carrier,
        warehouse=manifest.warehouse,
        status=manifest.status,
        attempts=manifest.attempts,
        last_error=manifest.last_error,
        created_at=manifest.created_at,
        sent_at=manifest.sent_at,
        order_numbers=order_numbers,
    )


def _order_numbers(session: Session, manifest: Manifest) -> list[str]:
    return [c.order_number for c in manifest_consignments(session, manifest)]


@router.post("/dispatch-confirmations", status_code=201)
def confirm_dispatch(
    payload: DispatchConfirmationIn, session: SessionDep
) -> DispatchConfirmationOut:
    """The confirmation is transactional: every named consignment must
    exist and be dispatchable, or nothing is dispatched - a partial
    confirmation silently splitting into shipped-and-not would be exactly
    the ambiguity the Manifest concept exists to remove."""
    duplicates = sorted(
        number for number, count in Counter(payload.order_numbers).items() if count > 1
    )
    if duplicates:
        raise HTTPException(
            422, f"order numbers repeat in the confirmation: {', '.join(duplicates)}"
        )
    rows = (
        session.execute(
            # Lock the rows for the confirmation's lifetime: two overlapping
            # confirmations for the same orders would otherwise both read
            # 'allocated', both dispatch, and declare the same consignments
            # on two manifests. The second now blocks, then sees 'dispatched'
            # and is rejected below. (A no-op on SQLite, which the unit suite
            # uses; the Postgres deployment is where the race is real.)
            select(Consignment)
            .where(Consignment.order_number.in_(payload.order_numbers))
            .with_for_update()
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
        manifests=[_manifest_out(m, _order_numbers(session, m)) for m in manifests],
    )


@router.get("/manifests")
def list_manifests(session: SessionDep) -> list[ManifestOut]:
    manifests = (
        session.execute(
            select(Manifest).order_by(Manifest.id.desc()).limit(MANIFEST_LIST_LIMIT)
        )
        .scalars()
        .all()
    )
    # One query for every listed manifest's order numbers, grouped in
    # Python, rather than a per-manifest lookup (an N+1 over a table that
    # only grows).
    rows = session.execute(
        select(ManifestConsignment.manifest_id, Consignment.order_number)
        .join(Consignment, Consignment.id == ManifestConsignment.consignment_id)
        .where(ManifestConsignment.manifest_id.in_([m.id for m in manifests]))
        .order_by(ManifestConsignment.id)
    ).all()
    orders: dict[int, list[str]] = {}
    for manifest_id, order_number in rows:
        orders.setdefault(manifest_id, []).append(order_number)
    return [_manifest_out(m, orders.get(m.id, [])) for m in manifests]


@router.get("/manifests/{manifest_id}")
def manifest_detail(manifest_id: int, session: SessionDep) -> ManifestOut:
    manifest = session.get(Manifest, manifest_id)
    if manifest is None:
        raise HTTPException(404, "no such manifest")
    return _manifest_out(manifest, _order_numbers(session, manifest))
