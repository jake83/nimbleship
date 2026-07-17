"""CRUD for Warehouses (CONTEXT.md: a logical dispatch identity, not
necessarily a physical building). A warehouse owns its collection days and
holidays; updates replace the whole record - the calendar is small config
data, not an event stream."""

import datetime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from nimbleship.db import get_session
from nimbleship.domain.collection import CollectionDays
from nimbleship.models import Warehouse, WarehouseCollectionDay, WarehouseHoliday

router = APIRouter(prefix="/warehouses", tags=["warehouses"])

SessionDep = Annotated[Session, Depends(get_session)]


class HolidayIn(BaseModel):
    date: datetime.date
    description: str | None = Field(default=None, max_length=255)


class WarehouseIn(BaseModel):
    # Codes appear in URL paths and WMS payloads: same safe alphabet as
    # order numbers.
    code: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=255)
    address_lines: list[str] = Field(min_length=1)
    postcode: str = Field(min_length=1, max_length=32)
    country: str = Field(min_length=2, max_length=3)
    # IANA name driving the warehouse's local dispatch day (e.g. its manifest
    # date). Required: a warehouse without a real timezone would silently fall
    # back to UTC and misdate a near-midnight manifest.
    timezone: str = Field(min_length=1, max_length=64)
    collection_days: CollectionDays = CollectionDays()
    holidays: list[HolidayIn] = []

    @field_validator("timezone")
    @classmethod
    def _timezone_is_a_known_zone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError(f"unknown timezone '{value}'") from error
        return value

    @model_validator(mode="after")
    def _holiday_dates_are_unique(self) -> "WarehouseIn":
        dates = [holiday.date for holiday in self.holidays]
        if len(dates) != len(set(dates)):
            raise ValueError("duplicate holiday dates")
        return self


class HolidayOut(BaseModel):
    date: datetime.date
    description: str | None


class WarehouseOut(BaseModel):
    code: str
    name: str
    company_name: str | None
    phone: str | None
    email: str | None
    address_lines: list[str]
    postcode: str
    country: str
    timezone: str
    collection_days: CollectionDays
    holidays: list[HolidayOut]


def _warehouse_out(warehouse: Warehouse) -> WarehouseOut:
    days = warehouse.collection_days
    return WarehouseOut(
        code=warehouse.code,
        name=warehouse.name,
        company_name=warehouse.company_name,
        phone=warehouse.phone,
        email=warehouse.email,
        address_lines=warehouse.address_lines,
        postcode=warehouse.postcode,
        country=warehouse.country,
        timezone=warehouse.timezone,
        collection_days=(
            CollectionDays.model_validate(days, from_attributes=True)
            if days is not None
            else CollectionDays()
        ),
        # Sorted here, not only by the relationship order_by: freshly created
        # rows are still in payload order before they round-trip the database.
        holidays=[
            HolidayOut(date=holiday.date, description=holiday.description)
            for holiday in sorted(warehouse.holidays, key=lambda h: h.date)
        ],
    )


def _apply(session: Session, payload: WarehouseIn, warehouse: Warehouse) -> None:
    warehouse.code = payload.code
    warehouse.name = payload.name
    warehouse.company_name = payload.company_name
    warehouse.phone = payload.phone
    warehouse.email = payload.email
    warehouse.address_lines = payload.address_lines
    warehouse.postcode = payload.postcode
    warehouse.country = payload.country
    warehouse.timezone = payload.timezone
    flags = payload.collection_days.model_dump()
    if warehouse.collection_days is None:
        warehouse.collection_days = WarehouseCollectionDay(**flags)
    else:
        for field, value in flags.items():
            setattr(warehouse.collection_days, field, value)
    if warehouse.holidays:
        # Clear-then-flush: replacing in one step would insert the new rows
        # before deleting the old ones, tripping the (warehouse_id, date)
        # unique constraint whenever a date is kept across the update.
        warehouse.holidays = []
        session.flush()
    warehouse.holidays = [
        WarehouseHoliday(date=holiday.date, description=holiday.description)
        for holiday in payload.holidays
    ]


def _code_taken(session: Session, code: str, ignore_id: int | None = None) -> bool:
    query = select(Warehouse.id).where(Warehouse.code == code)
    if ignore_id is not None:
        query = query.where(Warehouse.id != ignore_id)
    return session.execute(query).scalar_one_or_none() is not None


def _get_warehouse(session: Session, code: str) -> Warehouse:
    warehouse = session.execute(
        select(Warehouse)
        .options(selectinload(Warehouse.holidays))
        .where(Warehouse.code == code)
    ).scalar_one_or_none()
    if warehouse is None:
        raise HTTPException(404, "no warehouse with this code")
    return warehouse


@router.get("")
def list_warehouses(session: SessionDep) -> list[WarehouseOut]:
    warehouses = (
        session.execute(
            select(Warehouse)
            .options(selectinload(Warehouse.holidays))
            .order_by(Warehouse.code)
        )
        .scalars()
        .all()
    )
    return [_warehouse_out(warehouse) for warehouse in warehouses]


@router.post("", status_code=201)
def create_warehouse(payload: WarehouseIn, session: SessionDep) -> WarehouseOut:
    if _code_taken(session, payload.code):
        raise HTTPException(409, "a warehouse already exists with this code")
    warehouse = Warehouse()
    _apply(session, payload, warehouse)
    session.add(warehouse)
    try:
        session.flush()
    except IntegrityError as error:
        # Losing a duplicate race: the unique constraint backs the pre-check.
        raise HTTPException(409, "a warehouse already exists with this code") from error
    return _warehouse_out(warehouse)


@router.get("/{code}")
def warehouse_detail(code: str, session: SessionDep) -> WarehouseOut:
    return _warehouse_out(_get_warehouse(session, code))


@router.put("/{code}")
def update_warehouse(
    code: str, payload: WarehouseIn, session: SessionDep
) -> WarehouseOut:
    warehouse = _get_warehouse(session, code)
    if payload.code != code and _code_taken(session, payload.code, warehouse.id):
        raise HTTPException(409, "a warehouse already exists with this code")
    _apply(session, payload, warehouse)
    try:
        session.flush()
    except IntegrityError as error:
        raise HTTPException(409, "a warehouse already exists with this code") from error
    return _warehouse_out(warehouse)


@router.delete("/{code}", status_code=204)
def delete_warehouse(code: str, session: SessionDep) -> Response:
    session.delete(_get_warehouse(session, code))
    session.flush()
    return Response(status_code=204)
