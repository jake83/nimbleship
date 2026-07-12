"""The png_pages label pipeline: base64 PNG strings, one per parcel, become
a single printable PDF with one label per A6 page."""

import base64
import io
import struct
import zlib

import pytest
from pypdf import PdfReader
from reportlab.lib.pagesizes import A6

from nimbleship.engine.labels_png import LabelPageError, assemble_png_pages


def png(width: int, height: int) -> bytes:
    """A minimal valid RGB PNG, hand-assembled so tests depend on nothing
    but the PNG spec."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data))
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x66\x33\x99" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def encoded_png(width: int = 40, height: int = 60) -> str:
    return base64.b64encode(png(width, height)).decode()


@pytest.mark.parametrize("count", [1, 2, 5])
def test_page_count_equals_input_count(count: int) -> None:
    pdf = assemble_png_pages([encoded_png() for _ in range(count)])

    assert len(PdfReader(io.BytesIO(pdf)).pages) == count


def test_pages_are_a6() -> None:
    pdf = assemble_png_pages([encoded_png(), encoded_png(80, 20)])

    width, height = A6
    for page in PdfReader(io.BytesIO(pdf)).pages:
        assert float(page.mediabox.width) == pytest.approx(width)
        assert float(page.mediabox.height) == pytest.approx(height)


def test_labels_of_mixed_dimensions_assemble() -> None:
    """Scaling adapts per label: portrait, landscape, and square inputs all
    land on the same A6 page."""
    pdf = assemble_png_pages(
        [encoded_png(40, 60), encoded_png(60, 40), encoded_png(50, 50)]
    )

    assert len(PdfReader(io.BytesIO(pdf)).pages) == 3


def test_invalid_base64_fails_loudly_naming_the_index() -> None:
    pages = [encoded_png(), "not!!!base64", encoded_png()]

    with pytest.raises(LabelPageError, match=r"label page 1 "):
        assemble_png_pages(pages)


def test_base64_that_is_not_an_image_fails_loudly_naming_the_index() -> None:
    pages = [encoded_png(), base64.b64encode(b"plainly not a PNG").decode()]

    with pytest.raises(LabelPageError, match=r"label page 1 "):
        assemble_png_pages(pages)


def test_an_empty_page_list_fails_loudly() -> None:
    with pytest.raises(LabelPageError, match="no label pages"):
        assemble_png_pages([])
