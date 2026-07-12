"""JPG label assembly: a carrier's base64 JPG label becomes a printable
PDF - two copies (one per side of the pallet) with the order number
overlaid so the warehouse can match paper to consignment."""

import base64
import io

import pytest
from PIL import Image
from pypdf import PdfReader

from nimbleship.engine.labels_jpg import assemble_label

MM_TO_PT = 72 / 25.4


def label_image_base64(width: int = 400, height: int = 300) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buffer, "JPEG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def test_the_pdf_carries_two_copies_of_the_label_image() -> None:
    pdf = assemble_label(label_image_base64(), "95000254580")

    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 2
    for page in reader.pages:
        assert len(page.images) == 1


def test_pages_are_landscape_148_by_105_mm() -> None:
    pdf = assemble_label(label_image_base64(), "95000254580")

    for page in PdfReader(io.BytesIO(pdf)).pages:
        assert page.mediabox.width == pytest.approx(148 * MM_TO_PT, abs=0.5)
        assert page.mediabox.height == pytest.approx(105 * MM_TO_PT, abs=0.5)


def test_the_order_number_overlays_every_copy() -> None:
    pdf = assemble_label(label_image_base64(), "95000254580")

    for page in PdfReader(io.BytesIO(pdf)).pages:
        assert "Order Number: 95000254580" in page.extract_text()


def test_assembly_is_deterministic() -> None:
    image = label_image_base64()

    assert assemble_label(image, "95000254580") == assemble_label(image, "95000254580")


def test_garbage_base64_fails_loudly() -> None:
    with pytest.raises(ValueError, match="base64"):
        assemble_label("not*base64!", "95000254580")


def test_non_jpeg_image_data_fails_loudly() -> None:
    not_a_jpeg = base64.b64encode(b"%PDF-1.7 pretending").decode("ascii")

    with pytest.raises(ValueError, match="JPEG"):
        assemble_label(not_a_jpeg, "95000254580")


def test_a_blank_order_number_fails_loudly() -> None:
    with pytest.raises(ValueError, match="order number"):
        assemble_label(label_image_base64(), "  ")
