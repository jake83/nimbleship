"""Delivery Charge structures and calculator: what the company charges the
customer (never confused with Delivery Cost, what a carrier charges the
company).

Ports the old system's charge rules: weight bands with scope precedence
area -> country -> all. The first scope with a band matching the shipment's
weight wins; within that scope the cheapest matching band prices the
shipment as base charge + additional charge per started increment over the
band minimum. One deliberate non-port: the old calculator matched ANY
country band once a shipping area was resolved (it dropped the country id
to null in the fallback); here country bands only ever price their own
country."""

from decimal import ROUND_CEILING, Decimal
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field, model_validator

if TYPE_CHECKING:
    from nimbleship.domain.model import Shipment


class ChargeBand(BaseModel):
    scope_type: Literal["all", "country", "area"]
    scope_code: str | None = None
    # Warehouse scoping needs chunk E; the field is carried but never read.
    warehouse: str | None = None
    min_weight_kg: Decimal = Field(ge=0)
    max_weight_kg: Decimal
    charge: Decimal = Field(ge=0)
    additional_charge: Decimal | None = Field(default=None, ge=0)
    # Increment size for the additional charge; omitted = per whole kg. The
    # old system stored 0 and silently coerced it to 1 at calculation time;
    # ambiguous rows are refused at authoring instead (ADR 0003 rails).
    additional_charge_per_kg: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _scope_code_matches_scope_type(self) -> "ChargeBand":
        if self.scope_type == "all":
            if self.scope_code is not None:
                raise ValueError("scope_code must be absent for scope_type 'all'")
        elif self.scope_code is None:
            raise ValueError(
                f"scope_code is required for scope_type '{self.scope_type}'"
            )
        return self

    @model_validator(mode="after")
    def _weight_range_is_ordered(self) -> "ChargeBand":
        if self.max_weight_kg < self.min_weight_kg:
            raise ValueError("max_weight_kg must not be below min_weight_kg")
        return self


def _band_charge(band: ChargeBand, weight_kg: Decimal) -> Decimal:
    """Base charge plus the additional charge for every started increment
    over the band minimum (a started kilogram counts in full, as the old
    calculator's ceil did)."""
    if band.additional_charge is None:
        return band.charge
    excess_kg = weight_kg - band.min_weight_kg
    if excess_kg <= 0:
        return band.charge
    increment_kg = band.additional_charge_per_kg or Decimal(1)
    increments = (excess_kg / increment_kg).to_integral_value(rounding=ROUND_CEILING)
    return band.charge + increments * band.additional_charge


def calculate_charge(
    bands: list[ChargeBand], shipment: "Shipment", areas: list[str]
) -> Decimal | None:
    """The Delivery Charge for a shipment, or None when no band applies
    (no charge is configured for this destination and weight)."""
    weight_kg = shipment.total_weight_kg
    scopes = (
        [b for b in bands if b.scope_type == "area" and b.scope_code in areas],
        [
            b
            for b in bands
            if b.scope_type == "country"
            and b.scope_code == shipment.destination_country
        ],
        [b for b in bands if b.scope_type == "all"],
    )
    for scoped in scopes:
        matching = [
            b for b in scoped if b.min_weight_kg <= weight_kg <= b.max_weight_kg
        ]
        if matching:
            return min(_band_charge(b, weight_kg) for b in matching)
    return None
