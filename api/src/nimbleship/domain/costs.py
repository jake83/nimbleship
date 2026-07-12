"""Delivery Cost structures: what a carrier charges the company.

Shapes port the old system's cost rules (weight bands, parcel bands, fuel
surcharge, dimension surcharge). Chunk C implements the calculator and wires
the total into the selection policy in place of the flat cost."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class CostBand(BaseModel):
    cost_type: Literal[
        "consignment_weight", "parcel_count", "fuel_surcharge", "longest_dimension"
    ]
    min_weight_kg: Decimal | None = None
    max_weight_kg: Decimal | None = None
    min_parcels: int | None = None
    max_parcels: int | None = None
    charge: Decimal | None = None
    additional_charge: Decimal | None = None
    percentage: Decimal | None = None
    over_dimension_cm: Decimal | None = None
