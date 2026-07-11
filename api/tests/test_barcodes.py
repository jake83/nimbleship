from nimbleship.barcodes import parcel_barcodes


def test_generates_one_barcode_per_parcel() -> None:
    barcodes = parcel_barcodes("95000254580", 3)

    assert len(barcodes) == 3
    assert barcodes[0] == "95000254580-0"
    assert barcodes[2] == "95000254580-2"


def test_single_parcel_consignment() -> None:
    assert parcel_barcodes("95000254580", 1) == ["95000254580-0"]
