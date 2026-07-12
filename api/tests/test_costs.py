"""Delivery Cost bands and the pure calculator (chunk C).

The semantics port the old system's DeliveryCostCalculator: weight bands
(base charge + additional per kg over the band minimum), parcel bands, fuel
surcharge as a percentage of the base carriage, and dimension surcharges
added after fuel. None means no applicable band - the service cannot be
costed (ADR 0007: flagged loudly by the selection policy, never skipped)."""

from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from nimbleship.domain.costs import (
    CostBand,
    DimensionSurchargeBand,
    FuelSurchargeBand,
    ParcelCountBand,
    WeightBand,
    calculate_cost,
)
from nimbleship.domain.model import Shipment

_BANDS = TypeAdapter(list[CostBand])


def shipment(**overrides: object) -> Shipment:
    defaults: dict[str, object] = {
        "order_number": "95000254580",
        "destination_country": "GB",
        "total_weight_kg": Decimal("10"),
        "parcel_count": 1,
    }
    defaults.update(overrides)
    return Shipment(**defaults)  # type: ignore[arg-type]


def weight_band(**overrides: object) -> WeightBand:
    defaults: dict[str, object] = {
        "cost_type": "consignment_weight",
        "min_weight_kg": Decimal("0"),
        "max_weight_kg": Decimal("30"),
        "charge": Decimal("5.00"),
    }
    defaults.update(overrides)
    return WeightBand(**defaults)  # type: ignore[arg-type]


# --- validation: the band type implies its fields ---


def test_weight_band_requires_its_range_and_charge() -> None:
    with pytest.raises(ValidationError):
        _BANDS.validate_python([{"cost_type": "consignment_weight", "charge": "5.00"}])


def test_weight_band_rejects_fields_of_other_band_types() -> None:
    with pytest.raises(ValidationError):
        _BANDS.validate_python(
            [
                {
                    "cost_type": "consignment_weight",
                    "min_weight_kg": "0",
                    "max_weight_kg": "30",
                    "charge": "5.00",
                    "percentage": "10",
                }
            ]
        )


def test_parcel_band_requires_its_range_and_charge() -> None:
    with pytest.raises(ValidationError):
        _BANDS.validate_python([{"cost_type": "parcel_count", "charge": "3.00"}])


def test_fuel_band_requires_a_percentage() -> None:
    with pytest.raises(ValidationError):
        _BANDS.validate_python([{"cost_type": "fuel_surcharge"}])


def test_fuel_band_rejects_weight_fields() -> None:
    with pytest.raises(ValidationError):
        _BANDS.validate_python(
            [
                {
                    "cost_type": "fuel_surcharge",
                    "percentage": "10",
                    "min_weight_kg": "0",
                }
            ]
        )


def test_dimension_band_requires_threshold_and_charge() -> None:
    with pytest.raises(ValidationError):
        _BANDS.validate_python([{"cost_type": "longest_dimension", "charge": "5.00"}])


def test_unknown_cost_type_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _BANDS.validate_python([{"cost_type": "carrier_pigeon", "charge": "5.00"}])


def test_bands_parse_from_stored_json_shapes() -> None:
    bands = _BANDS.validate_python(
        [
            {
                "cost_type": "consignment_weight",
                "min_weight_kg": "0",
                "max_weight_kg": "30",
                "charge": "5.00",
                "additional_charge": "0.50",
            },
            {
                "cost_type": "parcel_count",
                "min_parcels": 1,
                "max_parcels": 5,
                "charge": "3.00",
            },
            {"cost_type": "fuel_surcharge", "percentage": "12.5"},
            {
                "cost_type": "longest_dimension",
                "over_dimension_cm": "120",
                "charge": "7.50",
            },
        ]
    )

    assert [type(b) for b in bands] == [
        WeightBand,
        ParcelCountBand,
        FuelSurchargeBand,
        DimensionSurchargeBand,
    ]


# --- calculate_cost: weight bands ---


def test_weight_band_charges_its_base() -> None:
    cost = calculate_cost([weight_band()], shipment(total_weight_kg=Decimal("10")))

    assert cost == Decimal("5.00")


def test_weight_band_adds_additional_charge_per_kg_over_band_minimum() -> None:
    band = weight_band(
        min_weight_kg=Decimal("2"),
        max_weight_kg=Decimal("30"),
        charge=Decimal("5.00"),
        additional_charge=Decimal("0.50"),
    )

    cost = calculate_cost([band], shipment(total_weight_kg=Decimal("10")))

    assert cost == Decimal("9.00")  # 5.00 + 8kg over the 2kg minimum * 0.50


def test_fractional_excess_weight_is_charged_exactly() -> None:
    band = weight_band(
        min_weight_kg=Decimal("2"),
        additional_charge=Decimal("1.00"),
    )

    cost = calculate_cost([band], shipment(total_weight_kg=Decimal("2.5")))

    assert cost == Decimal("5.50")


def test_weight_outside_every_band_yields_none() -> None:
    band = weight_band(min_weight_kg=Decimal("0"), max_weight_kg=Decimal("30"))

    cost = calculate_cost([band], shipment(total_weight_kg=Decimal("45")))

    assert cost is None


def test_cheapest_matching_weight_band_wins() -> None:
    bands: list[CostBand] = [
        weight_band(charge=Decimal("9.00")),
        weight_band(charge=Decimal("6.00")),
    ]

    cost = calculate_cost(bands, shipment())

    assert cost == Decimal("6.00")


def test_no_bands_at_all_yields_none() -> None:
    assert calculate_cost([], shipment()) is None


# --- calculate_cost: parcel bands ---


def test_parcel_band_charges_base_plus_additional_per_parcel_over_minimum() -> None:
    band = ParcelCountBand(
        cost_type="parcel_count",
        min_parcels=1,
        max_parcels=10,
        charge=Decimal("3.00"),
        additional_charge=Decimal("1.50"),
    )

    cost = calculate_cost([band], shipment(parcel_count=4))

    assert cost == Decimal("7.50")  # 3.00 + 3 parcels over the minimum * 1.50


def test_weight_and_parcel_charges_accumulate() -> None:
    bands: list[CostBand] = [
        weight_band(charge=Decimal("5.00")),
        ParcelCountBand(
            cost_type="parcel_count",
            min_parcels=1,
            max_parcels=10,
            charge=Decimal("3.00"),
        ),
    ]

    cost = calculate_cost(bands, shipment())

    assert cost == Decimal("8.00")


def test_parcel_count_outside_every_band_still_costs_by_weight() -> None:
    bands: list[CostBand] = [
        weight_band(charge=Decimal("5.00")),
        ParcelCountBand(
            cost_type="parcel_count",
            min_parcels=5,
            max_parcels=10,
            charge=Decimal("3.00"),
        ),
    ]

    cost = calculate_cost(bands, shipment(parcel_count=1))

    assert cost == Decimal("5.00")


# --- calculate_cost: fuel surcharge ---


def test_fuel_surcharge_is_a_percentage_over_the_carriage_subtotal() -> None:
    bands: list[CostBand] = [
        weight_band(charge=Decimal("10.00")),
        FuelSurchargeBand(cost_type="fuel_surcharge", percentage=Decimal("10")),
    ]

    cost = calculate_cost(bands, shipment())

    assert cost == Decimal("11.00")


def test_fuel_surcharge_alone_is_no_basis_for_a_cost() -> None:
    bands: list[CostBand] = [
        FuelSurchargeBand(cost_type="fuel_surcharge", percentage=Decimal("10"))
    ]

    assert calculate_cost(bands, shipment()) is None


# --- calculate_cost: dimension surcharge ---


def test_dimension_surcharge_applies_over_the_threshold() -> None:
    bands: list[CostBand] = [
        weight_band(charge=Decimal("5.00")),
        DimensionSurchargeBand(
            cost_type="longest_dimension",
            over_dimension_cm=Decimal("120"),
            charge=Decimal("7.50"),
        ),
    ]

    cost = calculate_cost(bands, shipment(max_dimension_cm=Decimal("150")))

    assert cost == Decimal("12.50")


def test_dimension_at_the_threshold_is_not_surcharged() -> None:
    bands: list[CostBand] = [
        weight_band(charge=Decimal("5.00")),
        DimensionSurchargeBand(
            cost_type="longest_dimension",
            over_dimension_cm=Decimal("120"),
            charge=Decimal("7.50"),
        ),
    ]

    cost = calculate_cost(bands, shipment(max_dimension_cm=Decimal("120")))

    assert cost == Decimal("5.00")


def test_unknown_dimension_skips_the_surcharge() -> None:
    bands: list[CostBand] = [
        weight_band(charge=Decimal("5.00")),
        DimensionSurchargeBand(
            cost_type="longest_dimension",
            over_dimension_cm=Decimal("120"),
            charge=Decimal("7.50"),
        ),
    ]

    cost = calculate_cost(bands, shipment())

    assert cost == Decimal("5.00")


def test_dimension_surcharge_is_not_fuel_surcharged() -> None:
    """The old calculator applies fuel to the carriage subtotal only, then
    adds dimension surcharges on top - port that ordering exactly."""
    bands: list[CostBand] = [
        weight_band(charge=Decimal("10.00")),
        FuelSurchargeBand(cost_type="fuel_surcharge", percentage=Decimal("10")),
        DimensionSurchargeBand(
            cost_type="longest_dimension",
            over_dimension_cm=Decimal("120"),
            charge=Decimal("5.00"),
        ),
    ]

    cost = calculate_cost(bands, shipment(max_dimension_cm=Decimal("150")))

    assert cost == Decimal("16.00")  # (10 * 1.10) + 5, not (10 + 5) * 1.10
