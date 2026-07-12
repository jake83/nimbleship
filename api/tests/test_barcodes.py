from nimbleship.domain.barcodes import parcel_barcodes


def test_sequences_are_one_based_in_print_order() -> None:
    barcodes = parcel_barcodes("95000254580", 3)

    assert barcodes == [
        "95000254580-1",
        "95000254580-2",
        "95000254580-3",
    ]


def test_single_parcel_gets_sequence_one() -> None:
    assert parcel_barcodes("95000254580", 1) == ["95000254580-1"]


def test_zero_parcels_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError, match="at least one parcel"):
        parcel_barcodes("95000254580", 0)
