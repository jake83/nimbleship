from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from nimbleship.db import get_session
from nimbleship.domain.carrier_definition import (
    FACT_ROOTS,
    CarrierDefinition,
    operation_fact_roots,
)
from nimbleship.domain.definitions import (
    active_definition_row,
    carrier_config,
    create_draft,
    definition_for,
    get_version,
    list_versions,
    publish,
    upsert_carrier_config,
)
from nimbleship.domain.facts import shipment_facts
from nimbleship.engine.render import (
    RenderedStep,
    RenderedUpload,
    render_operation,
)
from nimbleship.models import Consignment

router = APIRouter(prefix="/carriers/{carrier}", tags=["definitions"])

SessionDep = Annotated[Session, Depends(get_session)]

REPLAY_DEFAULT_LIMIT = 50


class DraftIn(BaseModel):
    definition: CarrierDefinition
    author: str = Field(default="api", max_length=64)


class VersionOut(BaseModel):
    carrier: str
    version: int
    status: str
    author: str


class VersionDetailOut(VersionOut):
    created_at: datetime


class ActiveOut(BaseModel):
    version: int
    definition: CarrierDefinition


class ReplayIn(BaseModel):
    order_numbers: list[str] | None = Field(default=None, max_length=500)
    limit: int = Field(default=REPLAY_DEFAULT_LIMIT, ge=1, le=500)
    # When true, replay only consignments dispatched with this
    # definition's carrier. The default replays every recent consignment:
    # any historical shipment is a valid render input, whichever carrier
    # happened to dispatch it.
    only_this_carrier: bool = False


class Difference(BaseModel):
    step: str
    field: str
    active: str | None
    draft: str | None


class ReplayResultOut(BaseModel):
    order_number: str
    changed: bool
    differences: list[Difference]
    error: str | None = None


class ReplayOut(BaseModel):
    carrier: str
    version: int
    total: int
    changed: int
    results: list[ReplayResultOut]


@router.get("/definitions/active")
def active(carrier: str, session: SessionDep) -> ActiveOut:
    row = active_definition_row(session, carrier)
    if row is None:
        raise HTTPException(404, "no published definition for this carrier")
    return ActiveOut(version=row.version, definition=definition_for(row))


@router.get("/definitions/versions")
def versions(carrier: str, session: SessionDep) -> list[VersionDetailOut]:
    return [
        VersionDetailOut(
            carrier=row.carrier,
            version=row.version,
            status=row.status,
            author=row.author,
            created_at=row.created_at,
        )
        for row in list_versions(session, carrier)
    ]


@router.post("/definitions/drafts", status_code=201)
def create_draft_version(
    carrier: str, payload: DraftIn, session: SessionDep
) -> VersionOut:
    if payload.definition.carrier != carrier:
        raise HTTPException(422, "the definition's carrier must match the URL carrier")
    row = create_draft(session, payload.definition, payload.author)
    return VersionOut(
        carrier=row.carrier,
        version=row.version,
        status=row.status,
        author=row.author,
    )


def _render_gate(session: Session, carrier: str, definition: CarrierDefinition) -> None:
    """ADR 0009's publish gate: renders must succeed (diffs are the
    author's business; render errors are not) against recent consignments,
    for every declared operation - a broken track or cancel mapping must
    not publish behind a healthy book. Zero history passes trivially -
    there is nothing to render."""
    if not definition.operations:
        return
    config = carrier_config(session, carrier)
    recent = (
        session.execute(
            select(Consignment)
            .options(selectinload(Consignment.parcels))
            .order_by(Consignment.id.desc())
            .limit(20)
        )
        .scalars()
        .all()
    )
    # A batch manifest renders from a synthetic manifest over many
    # consignments, not one shipment, so shipment facts cannot gate it;
    # rendering it offline needs manifest facts and is a tracked follow-up. A
    # fan-out manifest renders per consignment from shipment.* facts, so it
    # gates like a book operation.
    shipment_operations = [
        op_name
        for op_name, operation in definition.operations.items()
        if operation_fact_roots(op_name, operation.fan_out) == FACT_ROOTS
    ]
    for consignment in recent:
        facts: dict[str, object] = {
            "shipment": shipment_facts(consignment),
            "config": config,
        }
        for operation in shipment_operations:
            try:
                render_operation(definition, operation, facts)
            except ValueError as error:
                raise HTTPException(
                    409,
                    f"publish refused: rendering operation '{operation}' "
                    f"against order {consignment.order_number} failed: {error}",
                ) from error


@router.post("/definitions/versions/{version}/publish")
def publish_version(carrier: str, version: int, session: SessionDep) -> VersionOut:
    row = get_version(session, carrier, version)
    if row is None:
        raise HTTPException(404, "no such definition version")
    _render_gate(session, carrier, definition_for(row))
    try:
        publish(session, row)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return VersionOut(
        carrier=row.carrier,
        version=row.version,
        status=row.status,
        author=row.author,
    )


def _flatten(rendered: RenderedStep) -> dict[str, str]:
    if isinstance(rendered, RenderedUpload):
        # An upload diffs by where it lands and what it contains.
        return {
            "content_type": rendered.content_type,
            "remote_path": rendered.remote_path,
            "filename": rendered.filename,
            "content": rendered.content,
        }
    flat: dict[str, str] = {
        "method": rendered.method,
        "url": rendered.url,
        "content_type": rendered.content_type,
    }
    for key, value in rendered.query.items():
        flat[f"query.{key}"] = value
    for key, value in rendered.headers.items():
        flat[f"headers.{key}"] = value
    for key, body_value in rendered.body.items():
        flat[f"body.{key}"] = (
            body_value if isinstance(body_value, str) else repr(body_value)
        )
    return flat


def _diff_renders(
    active_renders: list[RenderedStep], draft_renders: list[RenderedStep]
) -> list[Difference]:
    differences: list[Difference] = []
    active_by_step = {r.step: r for r in active_renders}
    draft_by_step = {r.step: r for r in draft_renders}
    for step in sorted(set(active_by_step) | set(draft_by_step)):
        active_flat = _flatten(active_by_step[step]) if step in active_by_step else {}
        draft_flat = _flatten(draft_by_step[step]) if step in draft_by_step else {}
        for field in sorted(set(active_flat) | set(draft_flat)):
            if active_flat.get(field) != draft_flat.get(field):
                differences.append(
                    Difference(
                        step=step,
                        field=field,
                        active=active_flat.get(field),
                        draft=draft_flat.get(field),
                    )
                )
    return differences


@router.post("/definitions/versions/{version}/replay")
def golden_replay(
    carrier: str, version: int, payload: ReplayIn, session: SessionDep
) -> ReplayOut:
    """Golden Replay (CONTEXT.md): render the draft's book operation for
    historical consignments and diff field-by-field against the active
    definition's renders - fully offline, no carrier contact.

    Baseline staging: until transports execute live calls there is no
    recorded-traffic corpus, so the active definition's renders are the
    baseline. When execution lands (Furdeco rung onward), recorded real
    requests become the preferred baseline per ADR 0009."""
    row = get_version(session, carrier, version)
    if row is None:
        raise HTTPException(404, "no such definition version")
    draft = definition_for(row)
    active_row = active_definition_row(session, carrier)
    if active_row is None:
        raise HTTPException(409, "no active definition to replay against")
    active = definition_for(active_row)
    config = carrier_config(session, carrier)

    query = (
        select(Consignment)
        .options(selectinload(Consignment.parcels))
        .order_by(Consignment.id.desc())
    )
    if payload.only_this_carrier:
        query = query.where(Consignment.carrier == carrier)
    if payload.order_numbers is not None:
        query = query.where(Consignment.order_number.in_(payload.order_numbers))
    else:
        query = query.limit(payload.limit)

    results: list[ReplayResultOut] = []
    for consignment in session.execute(query).scalars():
        facts: dict[str, object] = {
            "shipment": shipment_facts(consignment),
            "config": config,
        }
        try:
            active_renders = render_operation(active, "book", facts)
            draft_renders = render_operation(draft, "book", facts)
        except ValueError as error:
            results.append(
                ReplayResultOut(
                    order_number=consignment.order_number,
                    changed=True,
                    differences=[],
                    error=str(error),
                )
            )
            continue
        differences = _diff_renders(active_renders, draft_renders)
        results.append(
            ReplayResultOut(
                order_number=consignment.order_number,
                changed=bool(differences),
                differences=differences,
            )
        )

    return ReplayOut(
        carrier=carrier,
        version=version,
        total=len(results),
        changed=sum(1 for r in results if r.changed),
        results=results,
    )


@router.put("/config")
def put_config(
    carrier: str, payload: dict[str, object], session: SessionDep
) -> dict[str, str]:
    upsert_carrier_config(session, carrier, payload)
    return {"carrier": carrier, "status": "saved"}
