"""Drop Out: the carrier-less route where NimbleShip generates the
consignment paperwork itself (see CONTEXT.md carried-over term). One label
page per parcel, each carrying that parcel's Parcel Barcode as Code 128."""

import io

from pydantic import BaseModel
from reportlab.graphics.barcode import code128
from reportlab.lib.pagesizes import A6
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas

from nimbleship.domain.barcodes import parcel_barcodes


class LabelSender(BaseModel):
    """Sender details for the label's From block, drawn from the Warehouse
    (the logical dispatch identity) when the consignment names one."""

    name: str
    address_lines: list[str]
    postcode: str
    country: str


class LabelRequest(BaseModel):
    order_number: str
    recipient_name: str
    address_lines: list[str]
    postcode: str
    country: str
    parcel_count: int
    sender: LabelSender | None = None


def render_labels(request: LabelRequest) -> bytes:
    buffer = io.BytesIO()
    _, page_height = A6
    canvas = Canvas(buffer, pagesize=A6)
    barcodes = parcel_barcodes(request.order_number, request.parcel_count)

    for index, barcode_value in enumerate(barcodes, start=1):
        y = page_height - 15 * mm
        canvas.setFont("Helvetica-Bold", 14)
        canvas.drawString(10 * mm, y, "Drop Out")
        canvas.setFont("Helvetica", 10)
        y -= 8 * mm
        canvas.drawString(10 * mm, y, f"Parcel {index} of {request.parcel_count}")
        y -= 10 * mm
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(10 * mm, y, request.recipient_name)
        canvas.setFont("Helvetica", 10)
        for line in [*request.address_lines, request.postcode, request.country]:
            y -= 5 * mm
            canvas.drawString(10 * mm, y, line)

        if request.sender is not None:
            y -= 8 * mm
            canvas.setFont("Helvetica", 8)
            canvas.drawString(10 * mm, y, f"From: {request.sender.name}")
            sender_address = ", ".join(
                [
                    *request.sender.address_lines,
                    request.sender.postcode,
                    request.sender.country,
                ]
            )
            y -= 4 * mm
            canvas.drawString(10 * mm, y, sender_address)

        barcode = code128.Code128(barcode_value, barHeight=18 * mm, barWidth=0.4)
        barcode.drawOn(canvas, 10 * mm, 18 * mm)
        canvas.setFont("Helvetica", 9)
        canvas.drawString(10 * mm, 12 * mm, barcode_value)
        canvas.showPage()

    canvas.save()
    return buffer.getvalue()
