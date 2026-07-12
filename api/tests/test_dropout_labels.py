import io

from pypdf import PdfReader

from nimbleship.carriers.dropout import LabelRequest, LabelSender, render_labels


def request(parcel_count: int = 2) -> LabelRequest:
    return LabelRequest(
        order_number="95000254580",
        recipient_name="John Doe",
        address_lines=["10 Rue de la Paix", "Paris"],
        postcode="75002",
        country="FR",
        parcel_count=parcel_count,
    )


def test_renders_one_page_per_parcel() -> None:
    pdf = render_labels(request(parcel_count=3))

    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 3


def test_pages_carry_one_based_parcel_headings_and_barcodes() -> None:
    pdf = render_labels(request(parcel_count=2))

    reader = PdfReader(io.BytesIO(pdf))
    first = reader.pages[0].extract_text()
    second = reader.pages[1].extract_text()

    assert "Parcel 1 of 2" in first
    assert "95000254580-1" in first
    assert "Parcel 2 of 2" in second
    assert "95000254580-2" in second


def test_pages_carry_recipient_details() -> None:
    pdf = render_labels(request(parcel_count=1))

    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()

    assert "John Doe" in text
    assert "75002" in text


def test_every_page_carries_sender_details_when_present() -> None:
    with_sender = request(parcel_count=2).model_copy(
        update={
            "sender": LabelSender(
                name="Acme Fulfilment Ltd",
                address_lines=["Unit 5, Trading Estate"],
                postcode="LE1 1AA",
                country="GB",
            )
        }
    )

    pdf = render_labels(with_sender)

    for page in PdfReader(io.BytesIO(pdf)).pages:
        text = page.extract_text()
        assert "From: Acme Fulfilment Ltd" in text
        assert "Unit 5, Trading Estate" in text
        assert "LE1 1AA" in text


def test_labels_without_a_sender_carry_no_from_block() -> None:
    pdf = render_labels(request(parcel_count=1))

    assert "From" not in PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
