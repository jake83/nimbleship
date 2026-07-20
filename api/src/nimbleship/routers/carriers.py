"""The carriers catalog: the list the admin surfaces pick a carrier from. All
per-carrier routes (definitions, config) live on the definitions router."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from nimbleship.db import get_session
from nimbleship.domain.definitions import carrier_catalog

router = APIRouter(prefix="/carriers", tags=["carriers"])

SessionDep = Annotated[Session, Depends(get_session)]


class CarrierOut(BaseModel):
    carrier: str
    # The highest published definition version; None when nothing is published yet
    # (drafted-only, or config stored ahead of any definition).
    active_version: int | None


@router.get("")
def list_carriers(session: SessionDep) -> list[CarrierOut]:
    return [
        CarrierOut(carrier=carrier, active_version=version)
        for carrier, version in carrier_catalog(session)
    ]
