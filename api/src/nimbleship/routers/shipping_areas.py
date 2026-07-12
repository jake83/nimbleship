"""CRUD for Shipping Areas (CONTEXT.md): the named geography is the thing,
its postcode prefixes are the definition. No auth, matching the existing
surfaces; no delete yet - services may reference an area code from rulebook
data, so removal needs a story of its own."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from nimbleship.db import get_session
from nimbleship.models import PostcodeArea, ShippingArea

router = APIRouter(prefix="/shipping-areas", tags=["shipping-areas"])

SessionDep = Annotated[Session, Depends(get_session)]


class ShippingAreaFields(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    country: str = Field(min_length=2, max_length=3)
    prefixes: list[str] = Field(min_length=1)

    @field_validator("country")
    @classmethod
    def _normalise_country(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("prefixes")
    @classmethod
    def _normalise_prefixes(cls, value: list[str]) -> list[str]:
        """Prefixes are stored normalised (uppercase, trimmed): the
        resolver matches exactly against stored values, so normalisation
        must happen at the write edge, never at lookup time."""
        normalised = sorted({prefix.strip().upper() for prefix in value})
        for prefix in normalised:
            if not prefix:
                raise ValueError("a postcode prefix cannot be blank")
            if len(prefix) > 16:
                raise ValueError(f"postcode prefix too long: {prefix}")
        return normalised


class ShippingAreaIn(ShippingAreaFields):
    # Same character set as service codes: area codes are referenced from
    # rulebook data (areas_served/areas_blocked) and must stay URL-safe.
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class ShippingAreaOut(BaseModel):
    code: str
    name: str
    country: str
    prefixes: list[str]


def _area_out(area: ShippingArea) -> ShippingAreaOut:
    return ShippingAreaOut(
        code=area.code,
        name=area.name,
        country=area.country,
        prefixes=[row.prefix for row in area.prefixes],
    )


@router.get("")
def list_shipping_areas(session: SessionDep) -> list[ShippingAreaOut]:
    areas = session.execute(
        select(ShippingArea)
        .options(selectinload(ShippingArea.prefixes))
        .order_by(ShippingArea.code)
    ).scalars()
    return [_area_out(area) for area in areas]


@router.post("", status_code=201)
def create_shipping_area(
    payload: ShippingAreaIn, session: SessionDep
) -> ShippingAreaOut:
    area = ShippingArea(
        code=payload.code,
        name=payload.name,
        country=payload.country,
        prefixes=[PostcodeArea(prefix=prefix) for prefix in payload.prefixes],
    )
    session.add(area)
    try:
        session.flush()
    except IntegrityError as error:
        # The unique index is the last line of defence under concurrency,
        # same pattern as consignment creation.
        raise HTTPException(
            409, "a shipping area already exists with this code"
        ) from error
    return _area_out(area)


@router.put("/{code}")
def update_shipping_area(
    code: str, payload: ShippingAreaFields, session: SessionDep
) -> ShippingAreaOut:
    """Replace name, country, and the full prefix list. The code is the
    area's identity (rulebook declarations reference it) and never changes."""
    area = session.execute(
        select(ShippingArea)
        .options(selectinload(ShippingArea.prefixes))
        .where(ShippingArea.code == code)
    ).scalar_one_or_none()
    if area is None:
        raise HTTPException(404, "no shipping area with this code")
    area.name = payload.name
    area.country = payload.country
    # Flush the orphaned rows away before inserting the replacements: in one
    # flush SQLAlchemy orders inserts first, tripping the (area_id, prefix)
    # unique constraint whenever a prefix survives the replacement.
    area.prefixes.clear()
    session.flush()
    area.prefixes.extend(PostcodeArea(prefix=prefix) for prefix in payload.prefixes)
    session.flush()
    return _area_out(area)
