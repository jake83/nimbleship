"""Delivery Charge structures: what the company charges the customer.

Shapes port the old system's charge rules (weight bands with scope
precedence: area, then country, then all). Chunk D implements the calculator
and the checkout-facing pricing."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class ChargeBand(BaseModel):
    scope_type: Literal["all", "country", "area"]
    scope_code: str | None = None
    warehouse: str | None = None
    min_weight_kg: Decimal
    max_weight_kg: Decimal
    charge: Decimal
    additional_charge: Decimal | None = None
    additional_charge_per_kg: Decimal | None = None
