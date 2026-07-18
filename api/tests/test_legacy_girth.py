"""The Legacy Interface derives a consignment's max girth from the WMS's
per-parcel dimensions (ADR 0007): per parcel, the longest side plus twice the
other two, maxed across parcels. A missing/sentinel-zero/non-finite dimension
counts as 0, and None means no parcel carried any usable dimension."""

from decimal import Decimal

from nimbleship.legacy.paperwork_service import _max_girth_cm


def test_derives_girth_from_a_parcels_dimensions() -> None:
    created = {"parcels": [{"height_cm": "120", "width_cm": "80", "depth_cm": "60"}]}

    # longest 120, sum 260: 120 + 2*(260-120) = 400.
    assert _max_girth_cm(created) == Decimal("400")


def test_takes_the_max_girth_across_all_parcels() -> None:
    created = {
        "parcels": [
            {"height_cm": "100", "width_cm": "40", "depth_cm": "40"},  # 260
            {"height_cm": "60", "width_cm": "60", "depth_cm": "60"},  # 300
        ]
    }

    assert _max_girth_cm(created) == Decimal("300")


def test_ignores_the_consignment_level_max_dimension() -> None:
    # Girth is purely parcel-derived; the consignment maxDimension never feeds it.
    created = {
        "max_dimension_cm": "999",
        "parcels": [{"height_cm": "10", "width_cm": "10", "depth_cm": "10"}],
    }

    # longest 10, sum 30: 10 + 2*20 = 50 - unaffected by the 999.
    assert _max_girth_cm(created) == Decimal("50")


def test_a_missing_or_sentinel_zero_dimension_counts_as_zero() -> None:
    created = {"parcels": [{"height_cm": "60", "width_cm": "0", "depth_cm": None}]}

    # dims [60, 0, 0]: longest 60, sum 60, girth 60.
    assert _max_girth_cm(created) == Decimal("60")


def test_absent_when_no_parcel_carries_a_dimension() -> None:
    created = {"parcels": [{"height_cm": None, "width_cm": "0", "depth_cm": None}]}

    assert _max_girth_cm(created) is None


def test_non_finite_dimensions_count_as_zero_not_a_crash() -> None:
    # Decimal parses NaN/Infinity and comparing a NaN raises; a hostile or
    # malformed WMS value must degrade to 0, never propagate as a 500.
    created = {
        "parcels": [{"height_cm": "Infinity", "width_cm": "nan", "depth_cm": "1e0"}]
    }

    # Only the finite 1e0 (=1) survives: dims [0, 0, 1], girth 1.
    assert _max_girth_cm(created) == Decimal("1")
