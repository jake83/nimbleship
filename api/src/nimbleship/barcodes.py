def parcel_barcodes(order_number: str, parcel_count: int) -> list[str]:
    """Generate the Parcel Barcode for each parcel of a consignment.

    A Parcel Barcode is the order number, a dash, and the parcel's 1-based
    sequence in label-print order (e.g. 95000254580-2).
    """
    return [f"{order_number}-{sequence}" for sequence in range(parcel_count)]
