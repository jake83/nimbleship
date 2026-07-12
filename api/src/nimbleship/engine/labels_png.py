"""The `png_pages` label source (ADR 0009): carriers answering one base64
PNG image per parcel. Assembly is a pure function - encoded pages in, one
printable PDF out, one label per A6 page - so it tests offline and stays
independent of how an executor obtains the pages."""

import base64
import binascii
import io

from reportlab.lib.pagesizes import A6
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas


class LabelPageError(ValueError):
    """A label page that cannot become a printed page: undecodable base64,
    an unreadable image, or no pages at all. Always names the page index -
    a carrier answering garbage for parcel 3 of 5 must say so."""


def assemble_png_pages(pages: list[str]) -> bytes:
    """One PDF from base64 PNG label pages: page count equals input count,
    each label centred on an A6 page and scaled to fit while preserving its
    aspect ratio."""
    if not pages:
        raise LabelPageError("no label pages to assemble")
    page_width, page_height = A6
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=A6)
    for index, encoded in enumerate(pages):
        image = _read_image(index, encoded)
        image_width, image_height = image.getSize()
        scale = min(page_width / image_width, page_height / image_height)
        drawn_width = image_width * scale
        drawn_height = image_height * scale
        canvas.drawImage(
            image,
            x=(page_width - drawn_width) / 2,
            y=(page_height - drawn_height) / 2,
            width=drawn_width,
            height=drawn_height,
        )
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def _read_image(index: int, encoded: str) -> ImageReader:
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise LabelPageError(
            f"label page {index} is not valid base64: {error}"
        ) from error
    try:
        image = ImageReader(io.BytesIO(decoded))
        image.getSize()
    except Exception as error:
        raise LabelPageError(
            f"label page {index} is not a readable image: {error}"
        ) from error
    return image
