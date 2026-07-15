"""Dispatch through a live-API carrier: the book operation's http steps
execute at consignment creation, the tracking reference and carrier
barcodes land on the consignment, every request/response is recorded as
carrier traffic, and a failed carrier call is loud - 502, never a silent
success. The carrier here is the Furdeco example definition over an
httpx.MockTransport: zero real network."""

import base64
import json
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.http_client import get_http_client
from nimbleship.models import CarrierTraffic, Consignment

EXAMPLE = Path(__file__).parent.parent / "examples" / "furdeco.definition.json"

BOOKING_RESPONSE = (
    "<response>"
    "<success>Order Created</success>"
    "<carrier_reference>F12345678910</carrier_reference>"
    "<barcodes>001122334455667688, 123456789123456789</barcodes>"
    "</response>"
)

ERROR_RESPONSE = "<response><error>Postcode not covered</error></response>"

RULEBOOK_DRAFT = {
    "author": "jake",
    "services": [
        {
            "code": "FURDECO-2MAN",
            "carrier": "furdeco",
            "name": "Furdeco Two Man",
            "weight_min_kg": "0",
            "weight_max_kg": "999",
            "countries": ["GB"],
            "cost": "25.00",
            "tie_break_order": 1,
        }
    ],
}

CONSIGNMENT = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],
}


def _publish_furdeco(client: TestClient) -> None:
    definition = json.loads(EXAMPLE.read_text())
    response = client.put(
        "/api/carriers/furdeco/config",
        json={
            "api_key": "SECRET-KEY",
            "base_url": "https://api.furdeco.example/orders",
            "trading_name": "Acme Trading",
        },
    )
    assert response.status_code == 200
    response = client.post(
        "/api/carriers/furdeco/definitions/drafts",
        json={"author": "jake", "definition": definition},
    )
    assert response.status_code == 201
    version = response.json()["version"]
    response = client.post(
        f"/api/carriers/furdeco/definitions/versions/{version}/publish"
    )
    assert response.status_code == 200
    version = client.post("/api/rulebook/drafts", json=RULEBOOK_DRAFT).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def _carrier_answers(
    app: FastAPI, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def override() -> Iterator[httpx.Client]:
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            yield http_client

    app.dependency_overrides[get_http_client] = override


@pytest.fixture
def furdeco_client(app: FastAPI, client: TestClient) -> TestClient:
    _publish_furdeco(client)
    _carrier_answers(app, lambda request: httpx.Response(200, text=BOOKING_RESPONSE))
    return client


def test_booking_stores_tracking_reference_and_carrier_barcodes(
    furdeco_client: TestClient,
) -> None:
    response = furdeco_client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "allocated"
    assert body["carrier"] == "furdeco"
    assert body["tracking_reference"] == "F12345678910"

    detail = furdeco_client.get("/api/consignments/95000254580").json()
    assert detail["tracking_reference"] == "F12345678910"
    assert [p["carrier_barcode"] for p in detail["parcels"]] == [
        "001122334455667688",
        "123456789123456789",
    ]


def test_booking_adds_a_booked_event_with_the_step_outcomes(
    furdeco_client: TestClient,
) -> None:
    furdeco_client.post("/api/consignments", json=CONSIGNMENT)

    detail = furdeco_client.get("/api/consignments/95000254580").json()
    stages = [e["stage"] for e in detail["events"]]
    assert stages == ["allocated", "booked", "label_created"]
    [booked] = [e for e in detail["events"] if e["stage"] == "booked"]
    assert booked["detail"]["carrier"] == "furdeco"
    assert booked["detail"]["tracking_reference"] == "F12345678910"
    assert booked["detail"]["steps"] == [
        {"step": "save", "status": 200, "success": True}
    ]


def test_booking_still_renders_the_local_label(furdeco_client: TestClient) -> None:
    furdeco_client.post("/api/consignments", json=CONSIGNMENT)

    label = furdeco_client.get("/api/consignments/95000254580/label.pdf")

    assert label.status_code == 200
    assert label.content.startswith(b"%PDF")


def test_booking_records_the_traffic(app: FastAPI, furdeco_client: TestClient) -> None:
    furdeco_client.post("/api/consignments", json=CONSIGNMENT)

    with app.state.session_factory() as session:
        [row] = session.execute(select(CarrierTraffic)).scalars().all()
        assert row.carrier == "furdeco"
        assert row.order_number == "95000254580"
        assert row.step == "save"
        assert row.request["body"]["OrderNumber"] == "95000254580"
        assert row.request["query"] == {"action": "save", "key": "SECRET-KEY"}
        assert row.response_status == 200
        assert "F12345678910" in row.response_body


def test_a_failed_carrier_call_is_a_502_with_the_carrier_message(
    app: FastAPI, client: TestClient
) -> None:
    _publish_furdeco(client)
    _carrier_answers(app, lambda request: httpx.Response(200, text=ERROR_RESPONSE))

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 502
    assert response.json()["detail"] == "Postcode not covered"

    # Never a silent success: the consignment is kept, marked failed, with
    # the error on the timeline and the traffic recorded.
    detail = client.get("/api/consignments/95000254580").json()
    assert detail["status"] == "booking_failed"
    assert detail["label_url"] is None
    stages = [e["stage"] for e in detail["events"]]
    assert stages == ["allocated", "booking_failed"]
    [failed] = [e for e in detail["events"] if e["stage"] == "booking_failed"]
    assert failed["detail"]["error"] == "Postcode not covered"

    with app.state.session_factory() as session:
        [row] = session.execute(select(CarrierTraffic)).scalars().all()
        assert row.response_status == 200
        assert "Postcode not covered" in row.response_body
        consignment = session.execute(select(Consignment)).scalar_one()
        assert consignment.status == "booking_failed"


def test_extra_carrier_barcodes_are_kept_in_the_booked_event(
    app: FastAPI, client: TestClient
) -> None:
    three_barcodes = (
        "<response>"
        "<carrier_reference>F12345678910</carrier_reference>"
        "<barcodes>B-1, B-2, B-3</barcodes>"
        "</response>"
    )
    _publish_furdeco(client)
    _carrier_answers(app, lambda request: httpx.Response(200, text=three_barcodes))

    client.post("/api/consignments", json=CONSIGNMENT)

    detail = client.get("/api/consignments/95000254580").json()
    # Two parcels get the first two barcodes positionally; nothing is lost -
    # the full list lives on the booked event.
    assert [p["carrier_barcode"] for p in detail["parcels"]] == ["B-1", "B-2"]
    [booked] = [e for e in detail["events"] if e["stage"] == "booked"]
    assert booked["detail"]["barcodes"] == ["B-1", "B-2", "B-3"]


def test_a_stepless_carrier_records_no_traffic(
    app: FastAPI, client: TestClient
) -> None:
    # dropout books with local_render only: no http steps, no carrier call.
    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    assert response.json()["carrier"] == "dropout"
    assert response.json()["tracking_reference"] is None
    with app.state.session_factory() as session:
        assert session.execute(select(CarrierTraffic)).scalars().all() == []


B64_LABEL_DEFINITION = {
    "carrier": "labelcarrier",
    "name": "Label Carrier",
    "auth": {"scheme": "none"},
    "operations": {
        "book": {
            "steps": [
                {
                    "name": "labels",
                    "transport": "http",
                    "request": {
                        "method": "POST",
                        "url": "config.labels_url",
                        "content_type": "json",
                        "mapping": [
                            {"target": "order", "source": "shipment.order_number"}
                        ],
                    },
                    "response": {
                        "format": "json",
                        "success_when": {"path": "label_pdf"},
                        "extract": [
                            {"name": "tracking_reference", "path": "shipment_number"},
                            {"name": "label_pdf", "path": "label_pdf"},
                        ],
                    },
                }
            ],
            "label": {"source": "base64_pdf", "from_extract": "label_pdf"},
        }
    },
}

FAKE_PDF = b"%PDF-1.4 a real-looking label from the carrier"


def _label_carrier_response(label_pdf: str) -> str:
    return json.dumps({"shipment_number": "D-123", "label_pdf": label_pdf})


def _publish_label_carrier(client: TestClient) -> None:
    client.put(
        "/api/carriers/labelcarrier/config",
        json={"labels_url": "https://api.label.example/labels"},
    )
    version = client.post(
        "/api/carriers/labelcarrier/definitions/drafts",
        json={"author": "jake", "definition": B64_LABEL_DEFINITION},
    ).json()["version"]
    client.post(f"/api/carriers/labelcarrier/definitions/versions/{version}/publish")
    rulebook = {
        "author": "jake",
        "services": [
            {
                "code": "LC-STD",
                "carrier": "labelcarrier",
                "name": "Label Carrier Std",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "10.00",
                "tie_break_order": 1,
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=rulebook).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")


def test_a_base64_pdf_label_is_the_decoded_carrier_pdf(
    app: FastAPI, client: TestClient
) -> None:
    _publish_label_carrier(client)
    encoded = base64.b64encode(FAKE_PDF).decode()
    _carrier_answers(
        app, lambda request: httpx.Response(200, text=_label_carrier_response(encoded))
    )

    created = client.post("/api/consignments", json=CONSIGNMENT)
    assert created.status_code == 201
    assert created.json()["carrier"] == "labelcarrier"

    label = client.get("/api/consignments/95000254580/label.pdf")
    assert label.status_code == 200
    # The stored label is exactly the carrier's PDF, not a locally-rendered one.
    assert label.content == FAKE_PDF


def test_a_base64_label_that_is_not_a_pdf_fails_the_booking(
    app: FastAPI, client: TestClient
) -> None:
    _publish_label_carrier(client)
    not_a_pdf = base64.b64encode(b"<html>error page</html>").decode()
    _carrier_answers(
        app,
        lambda request: httpx.Response(200, text=_label_carrier_response(not_a_pdf)),
    )

    created = client.post("/api/consignments", json=CONSIGNMENT)
    assert created.status_code == 502
    assert "not a PDF" in created.text

    # The carrier already created the shipment, so the booking is preserved
    # rather than lost: the consignment persists as label_failed, with the
    # failure and the carrier traffic on record.
    detail = client.get("/api/consignments/95000254580").json()
    assert detail["status"] == "label_failed"
    assert detail["label_url"] is None
    stages = [e["stage"] for e in detail["events"]]
    assert stages == ["allocated", "booked", "label_failed"]
    with app.state.session_factory() as session:
        assert session.execute(select(CarrierTraffic)).scalars().all() != []

    # A retry does not double-book: the persisted consignment 409s the
    # duplicate submission.
    retry = client.post("/api/consignments", json=CONSIGNMENT)
    assert retry.status_code == 409


def test_a_label_failure_losing_a_duplicate_race_409s_not_500(
    app: FastAPI, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nimbleship.routers.consignments as consignments_module

    _publish_label_carrier(client)
    # A first booking persists the consignment for this order.
    encoded = base64.b64encode(FAKE_PDF).decode()
    _carrier_answers(
        app, lambda request: httpx.Response(200, text=_label_carrier_response(encoded))
    )
    assert client.post("/api/consignments", json=CONSIGNMENT).status_code == 201

    # A racing duplicate whose label also fails: the pre-check misses the
    # committed row, the carrier call succeeds, the label is bad, and the
    # preserve-booking commit hits the unique constraint. It must 409 (the
    # order is already recorded), not 500.
    not_a_pdf = base64.b64encode(b"<html>error</html>").decode()
    _carrier_answers(
        app,
        lambda request: httpx.Response(200, text=_label_carrier_response(not_a_pdf)),
    )
    monkeypatch.setattr(
        consignments_module, "_order_exists", lambda session, order_number: False
    )

    response = client.post("/api/consignments", json=CONSIGNMENT)
    assert response.status_code == 409


def test_a_line_wrapped_base64_label_still_decodes(
    app: FastAPI, client: TestClient
) -> None:
    # Server-side encoders often line-wrap base64 at 76 columns; a valid label
    # must not be rejected just because its JSON carries newlines.
    _publish_label_carrier(client)
    wrapped = "\n".join(textwrap.wrap(base64.b64encode(FAKE_PDF).decode(), 76))
    _carrier_answers(
        app, lambda request: httpx.Response(200, text=_label_carrier_response(wrapped))
    )

    created = client.post("/api/consignments", json=CONSIGNMENT)
    assert created.status_code == 201

    label = client.get("/api/consignments/95000254580/label.pdf")
    assert label.status_code == 200
    assert label.content == FAKE_PDF


# A carrier that requires the client to mint each parcel's SSCC before the book
# call and send it in the request. The book mapping loops the parcels and emits
# each minted code from item.carrier_barcode; the label is the returned PDF.
SSCC_DEFINITION = {
    "carrier": "ssccarrier",
    "name": "SSCC Carrier",
    "auth": {"scheme": "none"},
    "operations": {
        "book": {
            "allocate": [
                {
                    "kind": "sscc",
                    "per": "parcel",
                    "prefix": "config.sscc_prefix",
                    "policy": "halt",
                }
            ],
            "steps": [
                {
                    "name": "labels",
                    "transport": "http",
                    "request": {
                        "method": "POST",
                        "url": "config.labels_url",
                        "content_type": "json",
                        "mapping": [
                            {"target": "order", "source": "shipment.order_number"},
                            {
                                "target": "units",
                                "source": "shipment.parcels",
                                "each": [
                                    {"target": "sscc", "source": "item.carrier_barcode"}
                                ],
                            },
                        ],
                    },
                    "response": {
                        "format": "json",
                        "success_when": {"path": "label_pdf"},
                        "extract": [
                            {"name": "tracking_reference", "path": "shipment_number"},
                            {"name": "label_pdf", "path": "label_pdf"},
                        ],
                    },
                }
            ],
            "label": {"source": "base64_pdf", "from_extract": "label_pdf"},
        }
    },
}

# A 10-digit prefix leaves a 7-digit serial; the first two parcels mint serials
# 1 and 2, each closed by its GS1 mod-10 check digit.
SSCC_PREFIX = "0012345678"
SSCC_1 = "001234567800000019"
SSCC_2 = "001234567800000026"
SSCC_3 = "001234567800000033"
SSCC_4 = "001234567800000040"


def _sscc_response(label_pdf: str) -> str:
    return json.dumps({"shipment_number": "SS-1", "label_pdf": label_pdf})


def _publish_sscc_carrier(client: TestClient, prefix: str = SSCC_PREFIX) -> None:
    client.put(
        "/api/carriers/ssccarrier/config",
        json={"labels_url": "https://api.ssc.example/labels", "sscc_prefix": prefix},
    )
    version = client.post(
        "/api/carriers/ssccarrier/definitions/drafts",
        json={"author": "jake", "definition": SSCC_DEFINITION},
    ).json()["version"]
    published = client.post(
        f"/api/carriers/ssccarrier/definitions/versions/{version}/publish"
    )
    assert published.status_code == 200, published.text
    rulebook = {
        "author": "jake",
        "services": [
            {
                "code": "SS-STD",
                "carrier": "ssccarrier",
                "name": "SSCC Carrier Std",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "5.00",
                "tie_break_order": 1,
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=rulebook).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")


def _sscc_answers(app: FastAPI) -> None:
    encoded = base64.b64encode(FAKE_PDF).decode()
    _carrier_answers(
        app, lambda request: httpx.Response(200, text=_sscc_response(encoded))
    )


def test_per_parcel_ssccs_are_minted_and_stored_on_the_parcels(
    app: FastAPI, client: TestClient
) -> None:
    _publish_sscc_carrier(client)
    _sscc_answers(app)

    created = client.post("/api/consignments", json=CONSIGNMENT)
    assert created.status_code == 201
    assert created.json()["carrier"] == "ssccarrier"

    detail = client.get("/api/consignments/95000254580").json()
    # Two parcels, two distinct SSCCs from consecutive serials.
    assert [p["carrier_barcode"] for p in detail["parcels"]] == [SSCC_1, SSCC_2]


def test_the_book_request_carries_the_minted_ssccs(
    app: FastAPI, client: TestClient
) -> None:
    _publish_sscc_carrier(client)
    _sscc_answers(app)

    client.post("/api/consignments", json=CONSIGNMENT)

    with app.state.session_factory() as session:
        [row] = session.execute(select(CarrierTraffic)).scalars().all()
        # The carrier receives the codes this system minted, one per parcel.
        assert row.request["body"]["units"] == [{"sscc": SSCC_1}, {"sscc": SSCC_2}]


def test_ssccs_advance_across_bookings(app: FastAPI, client: TestClient) -> None:
    _publish_sscc_carrier(client)
    _sscc_answers(app)

    client.post("/api/consignments", json=CONSIGNMENT)
    second = dict(CONSIGNMENT, order_number="95000254581")
    client.post("/api/consignments", json=second)

    detail = client.get("/api/consignments/95000254581").json()
    # The range is durable: the second consignment mints the next serials, not
    # a fresh 1 and 2.
    assert [p["carrier_barcode"] for p in detail["parcels"]] == [SSCC_3, SSCC_4]


def test_an_exhausted_sscc_range_fails_the_booking_loudly(
    app: FastAPI, client: TestClient
) -> None:
    # A single-digit serial range holds serials 1-9; an eleven-parcel
    # consignment cannot mint all its codes.
    _publish_sscc_carrier(client, prefix="0123456789012345")
    _sscc_answers(app)
    eleven = dict(
        CONSIGNMENT, order_number="95000254590", parcels=[{"weight_kg": "1"}] * 11
    )

    created = client.post("/api/consignments", json=eleven)
    assert created.status_code == 503
    assert "exhausted" in created.text

    # All-or-nothing: nothing persisted, and the rolled-back mint burned no
    # serials - a following one-parcel booking still starts at serial 1.
    assert client.get("/api/consignments/95000254590").status_code == 404
    one_parcel = dict(
        CONSIGNMENT, order_number="95000254591", parcels=[{"weight_kg": "1"}]
    )
    ok = client.post("/api/consignments", json=one_parcel)
    assert ok.status_code == 201
    detail = client.get("/api/consignments/95000254591").json()
    assert detail["parcels"][0]["carrier_barcode"] == "012345678901234515"


def test_an_unconfigured_sscc_prefix_is_a_loud_config_error(
    app: FastAPI, client: TestClient
) -> None:
    _publish_sscc_carrier(client)
    _sscc_answers(app)
    # Drop the prefix the definition's allocate block names.
    client.put(
        "/api/carriers/ssccarrier/config",
        json={"labels_url": "https://api.ssc.example/labels"},
    )

    created = client.post("/api/consignments", json=CONSIGNMENT)
    assert created.status_code == 500
    assert "not configured" in created.text


def test_a_minted_sscc_is_not_overwritten_by_a_response_barcode(
    app: FastAPI, client: TestClient
) -> None:
    # A carrier that both mints SSCCs (allocate) and returns barcodes in its
    # response: the minted code is the one physically applied and sent, so a
    # response barcode must never clobber it.
    _publish_sscc_carrier(client)
    # A deep copy typed as Any so the nested extract list is indexable; add a
    # barcodes extraction to the otherwise-standard SSCC definition.
    with_barcodes = json.loads(json.dumps(SSCC_DEFINITION))
    with_barcodes["operations"]["book"]["steps"][0]["response"]["extract"].append(
        {"name": "barcodes", "path": "carrier_barcodes"}
    )
    version = client.post(
        "/api/carriers/ssccarrier/definitions/drafts",
        json={"author": "jake", "definition": with_barcodes},
    ).json()["version"]
    assert (
        client.post(
            f"/api/carriers/ssccarrier/definitions/versions/{version}/publish"
        ).status_code
        == 200
    )

    encoded = base64.b64encode(FAKE_PDF).decode()
    _carrier_answers(
        app,
        lambda request: httpx.Response(
            200,
            text=json.dumps(
                {
                    "shipment_number": "SS-1",
                    "label_pdf": encoded,
                    "carrier_barcodes": ["RESP-1", "RESP-2"],
                }
            ),
        ),
    )

    assert client.post("/api/consignments", json=CONSIGNMENT).status_code == 201

    detail = client.get("/api/consignments/95000254580").json()
    # The minted SSCCs survive; the response barcodes never clobber them.
    assert [p["carrier_barcode"] for p in detail["parcels"]] == [SSCC_1, SSCC_2]
    # The response barcodes are still kept on the booked event, losing nothing.
    [booked] = [e for e in detail["events"] if e["stage"] == "booked"]
    assert booked["detail"]["barcodes"] == ["RESP-1", "RESP-2"]
