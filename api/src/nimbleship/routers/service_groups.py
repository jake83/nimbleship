from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nimbleship.db import get_session
from nimbleship.domain.service_groups import (
    create_service_group,
    get_service_group,
    list_service_groups,
)
from nimbleship.models import ServiceGroup

router = APIRouter(prefix="/service-groups", tags=["service-groups"])

SessionDep = Annotated[Session, Depends(get_session)]

# ASCII word characters only, like order numbers: codes are stored inside
# rulebook JSON and quoted in traces, so keep them machine-safe.
CODE_PATTERN = r"^[A-Za-z0-9_-]+$"


class ServiceGroupFields(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=500)


class ServiceGroupIn(ServiceGroupFields):
    code: str = Field(min_length=1, max_length=64, pattern=CODE_PATTERN)


class ServiceGroupOut(BaseModel):
    code: str
    name: str
    description: str


def _out(row: ServiceGroup) -> ServiceGroupOut:
    return ServiceGroupOut(code=row.code, name=row.name, description=row.description)


@router.get("")
def service_groups(session: SessionDep) -> list[ServiceGroupOut]:
    return [_out(row) for row in list_service_groups(session)]


@router.post("", status_code=201)
def create(payload: ServiceGroupIn, session: SessionDep) -> ServiceGroupOut:
    try:
        row = create_service_group(
            session, payload.code, payload.name, payload.description
        )
    except IntegrityError as error:
        # Losing a duplicate race: the primary key is the last line of defence
        # behind the pre-check (the PR #6 consignments pattern).
        raise HTTPException(
            409, "a service group with this code already exists"
        ) from error
    if row is None:
        raise HTTPException(409, "a service group with this code already exists")
    return _out(row)


@router.put("/{code}")
def update(
    code: str, payload: ServiceGroupFields, session: SessionDep
) -> ServiceGroupOut:
    row = get_service_group(session, code)
    if row is None:
        raise HTTPException(404, "no such service group")
    row.name = payload.name
    row.description = payload.description
    session.flush()
    return _out(row)
