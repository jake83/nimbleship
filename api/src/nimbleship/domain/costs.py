"""Delivery Cost structures and calculator: what a carrier charges the
company (CONTEXT.md: Delivery Cost, never confused with Delivery Charge).

Band semantics: weight bands
(base charge plus an additional charge per kg over the band minimum), parcel
bands, a fuel surcharge percentage over the carriage subtotal, and dimension
surcharges added after fuel. The band type implies its fields: each type is
its own model and `cost_type` is the discriminator, so a band carrying
another type's fields fails validation at authoring time.

`calculate_cost` returning None means no weight or parcel band matched - the
service has no basis for a cost and the selection policy must exclude it
loudly (ADR 0007: missing cost data is flagged, never silently skipped)."""

from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from nimbleship.domain.model import Shipment


class WeightBand(BaseModel):
    """Base charge for a consignment-weight range, plus an additional
    charge per kg over the band minimum."""

    model_config = ConfigDict(extra="forbid")

    cost_type: Literal["consignment_weight"]
    min_weight_kg: Decimal
    max_weight_kg: Decimal
    charge: Decimal
    additional_charge: Decimal | None = None


class ParcelCountBand(BaseModel):
    """Base charge for a parcel-count range, plus an additional charge per
    parcel over the band minimum."""

    model_config = ConfigDict(extra="forbid")

    cost_type: Literal["parcel_count"]
    min_parcels: int
    max_parcels: int
    charge: Decimal
    additional_charge: Decimal | None = None


class FuelSurchargeBand(BaseModel):
    """A percentage applied to the carriage subtotal (weight + parcel
    charges), before dimension surcharges."""

    model_config = ConfigDict(extra="forbid")

    cost_type: Literal["fuel_surcharge"]
    percentage: Decimal


class DimensionSurchargeBand(BaseModel):
    """A flat charge added when the longest dimension exceeds the
    threshold, after the fuel surcharge."""

    model_config = ConfigDict(extra="forbid")

    cost_type: Literal["longest_dimension"]
    over_dimension_cm: Decimal
    charge: Decimal


type CostBand = Annotated[
    WeightBand | ParcelCountBand | FuelSurchargeBand | DimensionSurchargeBand,
    Field(discriminator="cost_type"),
]


def _weight_cost(band: WeightBand, weight_kg: Decimal) -> Decimal:
    excess_kg = max(Decimal("0"), weight_kg - band.min_weight_kg)
    return band.charge + excess_kg * (band.additional_charge or Decimal("0"))


def _parcel_cost(band: ParcelCountBand, parcel_count: int) -> Decimal:
    excess = max(0, parcel_count - band.min_parcels)
    return band.charge + excess * (band.additional_charge or Decimal("0"))


def calculate_cost(bands: list[CostBand], shipment: "Shipment") -> Decimal | None:
    """The Delivery Cost for a shipment under a service's cost bands, or
    None when no weight or parcel band matches (no basis for a cost).

    Where several bands of one kind match, the cheapest wins, so the
    result is band-order-blind (same rulebook, same shipment, same cost -
    regardless of how the bands are listed).

    An unknown longest dimension skips the surcharge (optimistic, ADR
    0007); dispatch re-calculates when the facts are complete."""
    weight_costs = [
        _weight_cost(band, shipment.total_weight_kg)
        for band in bands
        if isinstance(band, WeightBand)
        and band.min_weight_kg <= shipment.total_weight_kg <= band.max_weight_kg
    ]
    parcel_costs = [
        _parcel_cost(band, shipment.parcel_count)
        for band in bands
        if isinstance(band, ParcelCountBand)
        and shipment.parcel_count > 0
        and band.min_parcels <= shipment.parcel_count <= band.max_parcels
    ]
    if not weight_costs and not parcel_costs:
        return None

    carriage = min(weight_costs, default=Decimal("0")) + min(
        parcel_costs, default=Decimal("0")
    )

    fuel_percentage = sum(
        (band.percentage for band in bands if isinstance(band, FuelSurchargeBand)),
        Decimal("0"),
    )
    total = carriage + carriage * fuel_percentage / 100

    if shipment.max_dimension_cm is not None:
        total += sum(
            (
                band.charge
                for band in bands
                if isinstance(band, DimensionSurchargeBand)
                and shipment.max_dimension_cm > band.over_dimension_cm
            ),
            Decimal("0"),
        )
    return total
