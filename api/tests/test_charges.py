"""Delivery Charge calculator (chunk D): what the company charges the
customer. Ports the old DeliveryChargeCalculator semantics: scope precedence
area -> country -> all, first scope with a matching band wins, cheapest band
within that scope, base + additional per started kg over the band minimum."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from nimbleship.domain.charges import ChargeBand, calculate_charge
from nimbleship.domain.model import Shipment


def band(**overrides: object) -> ChargeBand:
    defaults: dict[str, object] = {
        "scope_type": "all",
        "min_weight_kg": Decimal("0"),
        "max_weight_kg": Decimal("30"),
        "charge": Decimal("4.99"),
    }
    defaults.update(overrides)
    return ChargeBand(**defaults)  # type: ignore[arg-type]


def shipment(**overrides: object) -> Shipment:
    defaults: dict[str, object] = {
        "order_number": "95000254580",
        "destination_country": "GB",
        "total_weight_kg": Decimal("10"),
        "parcel_count": 1,
    }
    defaults.update(overrides)
    return Shipment(**defaults)  # type: ignore[arg-type]


class TestChargeBandValidation:
    def test_area_scope_requires_a_scope_code(self) -> None:
        with pytest.raises(ValidationError, match="scope_code"):
            band(scope_type="area")

    def test_country_scope_requires_a_scope_code(self) -> None:
        with pytest.raises(ValidationError, match="scope_code"):
            band(scope_type="country")

    def test_all_scope_must_not_carry_a_scope_code(self) -> None:
        with pytest.raises(ValidationError, match="scope_code"):
            band(scope_type="all", scope_code="GB")

    def test_max_weight_below_min_weight_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="max_weight_kg"):
            band(min_weight_kg=Decimal("10"), max_weight_kg=Decimal("5"))

    def test_negative_min_weight_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            band(min_weight_kg=Decimal("-1"))

    def test_negative_charge_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            band(charge=Decimal("-0.01"))

    def test_negative_additional_charge_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            band(additional_charge=Decimal("-1"))

    def test_zero_additional_charge_per_kg_is_rejected(self) -> None:
        # The old system stored 0 and silently coerced it to 1 at calculation
        # time; NimbleShip refuses the ambiguous row at authoring instead.
        with pytest.raises(ValidationError):
            band(additional_charge_per_kg=Decimal("0"))

    def test_scoped_band_with_code_is_valid(self) -> None:
        assert band(scope_type="area", scope_code="SCOTTISH-HIGHLANDS").scope_code == (
            "SCOTTISH-HIGHLANDS"
        )


class TestCalculateCharge:
    def test_weight_at_band_minimum_pays_the_base_charge(self) -> None:
        bands = [
            band(
                min_weight_kg=Decimal("10"),
                max_weight_kg=Decimal("30"),
                charge=Decimal("8.00"),
                additional_charge=Decimal("1.50"),
            )
        ]

        result = calculate_charge(bands, shipment(total_weight_kg=Decimal("10")), [])

        assert result == Decimal("8.00")

    def test_excess_weight_adds_the_additional_charge_per_kg(self) -> None:
        bands = [
            band(
                min_weight_kg=Decimal("10"),
                max_weight_kg=Decimal("30"),
                charge=Decimal("8.00"),
                additional_charge=Decimal("1.50"),
            )
        ]

        result = calculate_charge(bands, shipment(total_weight_kg=Decimal("13")), [])

        assert result == Decimal("12.50")  # 8.00 + 3 * 1.50

    def test_a_started_kilogram_counts_in_full(self) -> None:
        bands = [
            band(
                min_weight_kg=Decimal("10"),
                max_weight_kg=Decimal("30"),
                charge=Decimal("8.00"),
                additional_charge=Decimal("1.50"),
            )
        ]

        result = calculate_charge(bands, shipment(total_weight_kg=Decimal("12.1")), [])

        assert result == Decimal("12.50")  # ceil(2.1) = 3 increments

    def test_additional_charge_per_kg_sets_the_increment_size(self) -> None:
        bands = [
            band(
                min_weight_kg=Decimal("10"),
                max_weight_kg=Decimal("30"),
                charge=Decimal("8.00"),
                additional_charge=Decimal("5.00"),
                additional_charge_per_kg=Decimal("5"),
            )
        ]

        result = calculate_charge(bands, shipment(total_weight_kg=Decimal("16")), [])

        assert result == Decimal("18.00")  # ceil(6/5) = 2 increments

    def test_no_additional_charge_means_base_only_over_minimum(self) -> None:
        bands = [
            band(
                min_weight_kg=Decimal("0"),
                max_weight_kg=Decimal("30"),
                charge=Decimal("4.99"),
            )
        ]

        result = calculate_charge(bands, shipment(total_weight_kg=Decimal("25")), [])

        assert result == Decimal("4.99")

    def test_weight_outside_every_band_yields_none(self) -> None:
        bands = [band(min_weight_kg=Decimal("0"), max_weight_kg=Decimal("30"))]

        result = calculate_charge(bands, shipment(total_weight_kg=Decimal("31")), [])

        assert result is None

    def test_no_bands_yields_none(self) -> None:
        assert calculate_charge([], shipment(), []) is None

    def test_area_band_wins_over_country_and_all(self) -> None:
        bands = [
            band(scope_type="all", charge=Decimal("4.99")),
            band(scope_type="country", scope_code="GB", charge=Decimal("5.99")),
            band(scope_type="area", scope_code="NI", charge=Decimal("14.99")),
        ]

        result = calculate_charge(bands, shipment(), ["NI"])

        assert result == Decimal("14.99")

    def test_country_band_wins_over_all_when_no_area_matches(self) -> None:
        bands = [
            band(scope_type="all", charge=Decimal("4.99")),
            band(scope_type="country", scope_code="GB", charge=Decimal("5.99")),
            band(scope_type="area", scope_code="NI", charge=Decimal("14.99")),
        ]

        result = calculate_charge(bands, shipment(), [])

        assert result == Decimal("5.99")

    def test_area_band_for_another_area_does_not_fire(self) -> None:
        bands = [
            band(scope_type="area", scope_code="NI", charge=Decimal("14.99")),
            band(scope_type="all", charge=Decimal("4.99")),
        ]

        result = calculate_charge(bands, shipment(), ["SCOTTISH-HIGHLANDS"])

        assert result == Decimal("4.99")

    def test_country_band_for_another_country_does_not_fire(self) -> None:
        # The old calculator matched ANY country band once an area was
        # resolved (scope id dropped to null); that quirk is not ported -
        # country bands only ever price their own country.
        bands = [
            band(scope_type="country", scope_code="FR", charge=Decimal("9.99")),
            band(scope_type="all", charge=Decimal("4.99")),
        ]

        result = calculate_charge(bands, shipment(destination_country="GB"), ["NI"])

        assert result == Decimal("4.99")

    def test_area_scope_without_matching_weight_falls_through(self) -> None:
        # First scope with a band matching the WEIGHT wins: an area band
        # whose weight range excludes the shipment does not block fallback.
        bands = [
            band(
                scope_type="area",
                scope_code="NI",
                min_weight_kg=Decimal("0"),
                max_weight_kg=Decimal("5"),
                charge=Decimal("14.99"),
            ),
            band(scope_type="all", charge=Decimal("4.99")),
        ]

        heavy = shipment(total_weight_kg=Decimal("10"))

        result = calculate_charge(bands, heavy, ["NI"])

        assert result == Decimal("4.99")

    def test_cheapest_band_within_the_winning_scope_wins(self) -> None:
        bands = [
            band(scope_type="all", charge=Decimal("6.99")),
            band(scope_type="all", charge=Decimal("4.99")),
        ]

        result = calculate_charge(bands, shipment(), [])

        assert result == Decimal("4.99")
