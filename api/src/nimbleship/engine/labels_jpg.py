"""JPG label assembly: some carriers answer a label request with a base64
JPG image. Warehouses print pallet labels two-up - one copy per side - so
the assembled PDF carries the image on two identical landscape 148x105mm
pages, each overlaid with the order number to match paper to consignment.

A pure function of its inputs: invalid base64, non-JPEG bytes, or a blank
order number fail loudly rather than producing an unprintable label."""

import base64
import binascii
import io

from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

PAGE_WIDTH = 148 * mm
PAGE_HEIGHT = 105 * mm
COPIES_PER_LABEL = 2

_JPEG_MAGIC = b"\xff\xd8\xff"


def assemble_label(image_base64: str, order_number: str) -> bytes:
    if not order_number.strip():
        raise ValueError("order number must not be blank")
    try:
        image_bytes = base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"label image is not valid base64: {error}") from error
    if not image_bytes.startswith(_JPEG_MAGIC):
        raise ValueError("label image is not a JPEG")

    image = ImageReader(io.BytesIO(image_bytes))
    image_width, image_height = image.getSize()
    if image_width <= 0 or image_height <= 0:
        raise ValueError("label image has no size")

    buffer = io.BytesIO()
    # invariant strips timestamps and randomised IDs: the same label bytes
    # always assemble to the same PDF, so replays diff cleanly.
    canvas = Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT), invariant=True)
    for _ in range(COPIES_PER_LABEL):
        canvas.drawImage(
            image,
            0,
            0,
            width=PAGE_WIDTH,
            height=PAGE_HEIGHT,
            preserveAspectRatio=True,
            anchor="c",
        )
        canvas.setFont("Helvetica-Bold", 7)
        canvas.setFillColorRGB(0, 0, 0)
        canvas.drawString(7 * mm, 9 * mm, f"Order Number: {order_number}")
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()
