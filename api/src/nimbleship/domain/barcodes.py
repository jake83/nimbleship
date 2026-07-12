def parcel_barcodes(order_number: str, parcel_count: int) -> list[str]:
    """Generate the Parcel Barcode for each parcel of a consignment.

    Per CONTEXT.md: the order number, a dash, and the parcel's 1-based
    sequence in label-print order (e.g. 95000254580-2).
    """
    if parcel_count < 1:
        raise ValueError("a consignment has at least one parcel")
    return [f"{order_number}-{sequence}" for sequence in range(1, parcel_count + 1)]
