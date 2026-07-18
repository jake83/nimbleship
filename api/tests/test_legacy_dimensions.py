"""The Legacy Interface derives a consignment's max dimension from the WMS's
per-parcel dimensions (ADR 0007): the consignment-level `maxDimension` it sends
is almost always the sentinel 0, so the real value is the largest single parcel
dimension, with 0/absent/non-finite treated as absent."""

from decimal import Decimal

from nimbleship.legacy.paperwork_service import _max_dimension_cm


def test_uses_the_consignment_value_when_provided() -> None:
    created = {"max_dimension_cm": "150", "parcels": []}

    assert _max_dimension_cm(created) == Decimal("150")


def test_derives_from_parcel_dimensions_when_consignment_is_the_sentinel_zero() -> None:
    created = {
        "max_dimension_cm": "0",
        "parcels": [
            {"height_cm": "120", "width_cm": "80", "depth_cm": "60"},
        ],
    }

    # The largest single dimension of the parcel.
    assert _max_dimension_cm(created) == Decimal("120")


def test_takes_the_max_across_all_parcels() -> None:
    created = {
        "max_dimension_cm": "0",
        "parcels": [
            {"height_cm": "100", "width_cm": "40", "depth_cm": "40"},
            {"height_cm": "30", "width_cm": "30", "depth_cm": "140"},
        ],
    }

    assert _max_dimension_cm(created) == Decimal("140")


def test_takes_the_max_of_consignment_and_parcel_dimensions() -> None:
    created = {
        "max_dimension_cm": "90",
        "parcels": [{"height_cm": "120", "width_cm": "10", "depth_cm": "10"}],
    }

    assert _max_dimension_cm(created) == Decimal("120")


def test_absent_when_nothing_is_provided() -> None:
    created = {
        "max_dimension_cm": "0",
        "parcels": [{"height_cm": None, "width_cm": "0", "depth_cm": None}],
    }

    assert _max_dimension_cm(created) is None


def test_non_finite_dimensions_are_absent_not_a_crash() -> None:
    # Decimal parses NaN/Infinity, and comparing a NaN raises; a hostile or
    # malformed WMS value must be treated as absent, never propagate as a 500.
    created = {
        "max_dimension_cm": "NaN",
        "parcels": [{"height_cm": "Infinity", "width_cm": "nan", "depth_cm": "1e0"}],
    }

    # Only the finite 1e0 (=1) survives.
    assert _max_dimension_cm(created) == Decimal("1")


def test_a_dimension_too_wide_for_the_column_degrades_to_none() -> None:
    # An absurd WMS dimension whose string overflows the column degrades to None
    # (unknown) rather than reaching Postgres as an uncaught StringDataRightTruncation
    # - the same guard girth already has.
    created = {"parcels": [{"height_cm": "999999999999999999", "width_cm": "1"}]}

    # 18 digits, wider than the 16-char column.
    assert _max_dimension_cm(created) is None
