from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nimbleship.db import get_session
from nimbleship.domain.propositions import (
    create_proposition,
    get_proposition,
    list_propositions,
)
from nimbleship.models import DeliveryProposition

router = APIRouter(prefix="/propositions", tags=["propositions"])

SessionDep = Annotated[Session, Depends(get_session)]

# ASCII word characters only, like order numbers: codes are stored inside
# rulebook JSON and quoted in traces, so keep them machine-safe.
CODE_PATTERN = r"^[A-Za-z0-9_-]+$"


class PropositionFields(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=500)


class PropositionIn(PropositionFields):
    code: str = Field(min_length=1, max_length=64, pattern=CODE_PATTERN)


class PropositionOut(BaseModel):
    code: str
    name: str
    description: str


def _out(row: DeliveryProposition) -> PropositionOut:
    return PropositionOut(code=row.code, name=row.name, description=row.description)


@router.get("")
def propositions(session: SessionDep) -> list[PropositionOut]:
    return [_out(row) for row in list_propositions(session)]


@router.post("", status_code=201)
def create(payload: PropositionIn, session: SessionDep) -> PropositionOut:
    try:
        row = create_proposition(
            session, payload.code, payload.name, payload.description
        )
    except IntegrityError as error:
        # Losing a duplicate race: the primary key is the last line of
        # defence behind the pre-check (the PR #6 consignments pattern).
        raise HTTPException(
            409, "a proposition with this code already exists"
        ) from error
    if row is None:
        raise HTTPException(409, "a proposition with this code already exists")
    return _out(row)


@router.put("/{code}")
def update(
    code: str, payload: PropositionFields, session: SessionDep
) -> PropositionOut:
    row = get_proposition(session, code)
    if row is None:
        raise HTTPException(404, "no such proposition")
    row.name = payload.name
    row.description = payload.description
    session.flush()
    return _out(row)
