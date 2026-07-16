"""The WMS-facing Legacy Interface (ADR 0002, ADR 0011): a SOAP/XML edge over
the same domain operations as the JSON API. The fixture here is a synthetic,
MetaPack-shaped request (real recorded traffic replaces it as operations land)."""

import base64
import json
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.http_client import get_http_client
from nimbleship.models import Consignment, LegacyConsignmentStaging

WMS_USER = "wms"
WMS_PASSWORD = "s3cret"

_FIXTURES = Path(__file__).parent / "fixtures" / "metapack"


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


@pytest.fixture
def wms_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, str]]:
    # get_settings reads the environment per request, so setting the credentials
    # after the app is built still gates the very next call.
    monkeypatch.setenv("NIMBLESHIP_LEGACY_WMS_USERNAME", WMS_USER)
    monkeypatch.setenv("NIMBLESHIP_LEGACY_WMS_PASSWORD", WMS_PASSWORD)
    yield (WMS_USER, WMS_PASSWORD)


def test_the_edge_rejects_a_request_with_no_credentials(client: TestClient) -> None:
    response = client.post(
        "/ConsignmentService",
        content=b"<soap:Envelope/>",
        headers={"Content-Type": "text/xml"},
    )

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_the_edge_rejects_wrong_credentials(
    client: TestClient, wms_auth: tuple[str, str]
) -> None:
    response = client.post(
        "/ConsignmentService",
        content=b"<soap:Envelope/>",
        headers={"Content-Type": "text/xml"},
        auth=(WMS_USER, "wrong-password"),
    )

    assert response.status_code == 401


def test_the_edge_rejects_all_requests_when_unconfigured(client: TestClient) -> None:
    # No credential configured: the edge is closed, even with a Basic header, so
    # a fresh install never exposes the WMS surface by omission.
    response = client.post(
        "/ConsignmentService",
        content=b"<soap:Envelope/>",
        headers={"Content-Type": "text/xml"},
        auth=(WMS_USER, WMS_PASSWORD),
    )

    assert response.status_code == 401


def test_create_consignments_stages_the_shipment_and_returns_a_code(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    response = client.post(
        "/ConsignmentService",
        content=_fixture("create_consignments_request.xml"),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    # The WMS reads the assigned consignment code and Unallocated status back,
    # and reuses the code on the allocate and paperwork calls.
    assert "95000254580" in response.text
    assert "Unallocated" in response.text

    with app.state.session_factory() as session:
        rows = list(session.execute(select(LegacyConsignmentStaging)).scalars())
    assert len(rows) == 1
    staged = rows[0]
    assert staged.order_number == "95000254580"
    assert staged.consignment_code in response.text
    created = staged.created_data
    assert created is not None
    assert created["recipient_name"] == "Jane Doe"
    assert created["postcode"] == "SW1A 2AA"
    assert created["destination_country"] == "GB"
    assert created["warehouse"] == "DEPOT1"
    assert len(created["parcels"]) == 2


def test_create_consignments_faults_and_stages_nothing_without_an_order_number(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A consignment with no orderNumber is rejected with a fault, and the batch
    # rolls back so nothing is staged under a placeholder key.
    body = _fixture("create_consignments_request.xml").replace(
        b'<orderNumber xsi:type="xsd:string">95000254580</orderNumber>', b""
    )

    response = client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "Fault" in response.text
    assert "orderNumber" in response.text
    with app.state.session_factory() as session:
        assert not list(session.execute(select(LegacyConsignmentStaging)).scalars())


_TWO_ITEMS_SAME_ORDER = (
    b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
    b' xmlns:soapenc="http://schemas.xmlsoap.org/soap/encoding/"'
    b' xmlns:tns="urn:DeliveryManager/services">'
    b"<soap:Body>"
    b'<tns:createConsignments><consignments href="#id1"/></tns:createConsignments>'
    b'<soapenc:Array id="id1"><Item href="#id2"/><Item href="#id3"/></soapenc:Array>'
    b'<q:Consignment id="id2" xmlns:q="urn:DeliveryManager/types">'
    b"<orderNumber>SAME-ORDER</orderNumber></q:Consignment>"
    b'<q:Consignment id="id3" xmlns:q="urn:DeliveryManager/types">'
    b"<orderNumber>SAME-ORDER</orderNumber></q:Consignment>"
    b"</soap:Body></soap:Envelope>"
)


def test_create_consignments_faults_on_a_duplicate_order_in_one_batch(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # Two items in one batch sharing an order number are distinct shipments
    # colliding; faulting keeps the second from overwriting the first's row and
    # handing both the same code. The whole batch rolls back.
    response = client.post(
        "/ConsignmentService",
        content=_TWO_ITEMS_SAME_ORDER,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "duplicate orderNumber" in response.text
    with app.state.session_factory() as session:
        assert not list(session.execute(select(LegacyConsignmentStaging)).scalars())


def test_malformed_xml_returns_a_soap_fault(
    client: TestClient, wms_auth: tuple[str, str]
) -> None:
    response = client.post(
        "/ConsignmentService",
        content=b"<soap:Envelope><unclosed>",
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "Fault" in response.text


def test_a_forbidden_xml_entity_returns_a_soap_fault_not_a_500(
    client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # defusedxml blocks the entity declaration (the XXE/expansion attack class);
    # the edge must still answer with the dialect's fault, not an unhandled 500.
    body = (
        b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "x">]>'
        b'<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        b"<soap:Body/></soap:Envelope>"
    )

    response = client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "Fault" in response.text


def test_the_edge_rejects_an_oversized_body(
    client: TestClient, wms_auth: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nimbleship.legacy.router._MAX_BODY_BYTES", 100)

    response = client.post(
        "/ConsignmentService",
        content=b"x" * 200,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 413


def _allocate_body(codes: list[str], service_groups: list[str]) -> bytes:
    code_items = "".join(f"<Item>{code}</Item>" for code in codes)
    group_items = "".join(f"<Item>{group}</Item>" for group in service_groups)
    return (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:soapenc="http://schemas.xmlsoap.org/soap/encoding/"'
        ' xmlns:tns="urn:DeliveryManager/services">'
        "<soap:Body>"
        '<tns:allocateConsignments><consignmentCodes href="#id1"/>'
        '<filter href="#id2"/></tns:allocateConsignments>'
        f'<soapenc:Array id="id1">{code_items}</soapenc:Array>'
        '<q:AllocationFilter id="id2" xmlns:q="urn:DeliveryManager/types">'
        '<acceptableCarrierServiceGroupCodes href="#id3"/></q:AllocationFilter>'
        f'<soapenc:Array id="id3">{group_items}</soapenc:Array>'
        "</soap:Body></soap:Envelope>"
    ).encode()


def _stage_a_consignment(
    client: TestClient, auth: tuple[str, str], app: FastAPI
) -> str:
    client.post(
        "/ConsignmentService",
        content=_fixture("create_consignments_request.xml"),
        headers={"Content-Type": "text/xml"},
        auth=auth,
    )
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        code = row.consignment_code
        assert isinstance(code, str)
        return code


def test_allocate_consignments_stages_service_groups_and_returns_allocated(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    code = _stage_a_consignment(client, wms_auth, app)

    response = client.post(
        "/AllocationService",
        content=_allocate_body([code], ["NEXTDAY"]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 200
    assert "Allocated" in response.text
    assert code in response.text
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        assert row.allocation_data == {"service_group_codes": ["NEXTDAY"]}


def test_allocate_consignments_faults_on_a_code_that_was_never_created(
    client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A code only exists once create minted it, so allocating an unknown one is a
    # lifecycle error, not a silent no-op.
    response = client.post(
        "/AllocationService",
        content=_allocate_body(["NS9999999"], ["NEXTDAY"]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "unknown consignmentCode" in response.text


def test_allocate_consignments_with_no_service_group_filter_still_allocates(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    code = _stage_a_consignment(client, wms_auth, app)

    response = client.post(
        "/AllocationService",
        content=_allocate_body([code], []),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 200
    assert "Allocated" in response.text
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        assert row.allocation_data == {"service_group_codes": []}


def test_allocate_rolls_back_the_whole_batch_when_one_code_is_unknown(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A valid code before an unknown one in the batch must not keep its
    # allocation: the fault rolls the whole batch back, like createConsignments.
    code = _stage_a_consignment(client, wms_auth, app)

    response = client.post(
        "/AllocationService",
        content=_allocate_body([code, "NS9999999"], ["NEXTDAY"]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "unknown consignmentCode" in response.text
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        assert row.allocation_data is None


def test_allocate_consignments_faults_on_a_duplicate_code_in_one_batch(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    code = _stage_a_consignment(client, wms_auth, app)

    response = client.post(
        "/AllocationService",
        content=_allocate_body([code, code], ["NEXTDAY"]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "duplicate consignmentCode" in response.text
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        assert row.allocation_data is None


def test_allocate_consignments_faults_on_a_blank_code_rather_than_dropping_it(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A valid code alongside a blank Item must fault, not silently allocate only
    # the valid one and drop the blank - that would tell the WMS a shipment it
    # never named was accepted.
    code = _stage_a_consignment(client, wms_auth, app)

    response = client.post(
        "/AllocationService",
        content=_allocate_body([code, ""], ["NEXTDAY"]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "blank consignmentCode" in response.text
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        assert row.allocation_data is None


_XSI = "http://www.w3.org/2001/XMLSchema-instance"


def _localname(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _paperwork_return(xml: str) -> ET.Element:
    """The single Paperwork object the WMS reads its labels and parcels from."""
    root = ET.fromstring(xml)
    paperwork = next(
        element
        for element in root.iter()
        if _localname(element) == "createPaperworkForConsignmentsReturn"
    )
    return paperwork


def _child_text(parent: ET.Element, name: str) -> str | None:
    child = parent.find(name)
    assert child is not None, f"missing <{name}>"
    return child.text


def _paperwork_body(codes: list[str]) -> bytes:
    code_items = "".join(f"<Item>{code}</Item>" for code in codes)
    return (
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns:soapenc="http://schemas.xmlsoap.org/soap/encoding/"'
        ' xmlns:tns="urn:DeliveryManager/services">'
        "<soap:Body>"
        "<tns:createPaperworkForConsignments>"
        '<consignmentCodes href="#id1"/>'
        "</tns:createPaperworkForConsignments>"
        f'<soapenc:Array id="id1">{code_items}</soapenc:Array>'
        "</soap:Body></soap:Envelope>"
    ).encode()


def _create_depot1(client: TestClient) -> None:
    # The create fixture's senderCode; the domain resolves it to a Warehouse or
    # rejects the code, so paperwork's happy path needs the row to exist.
    response = client.post(
        "/api/warehouses",
        json={
            "code": "DEPOT1",
            "name": "Depot 1",
            "address_lines": ["1 Dock Road"],
            "postcode": "M1 1AA",
            "country": "GB",
        },
    )
    assert response.status_code == 201


def _stage_and_allocate(client: TestClient, auth: tuple[str, str], app: FastAPI) -> str:
    code = _stage_a_consignment(client, auth, app)
    response = client.post(
        "/AllocationService",
        content=_allocate_body([code], ["ECONOMY"]),
        headers={"Content-Type": "text/xml"},
        auth=auth,
    )
    assert response.status_code == 200
    return code


def test_paperwork_runs_the_domain_and_returns_labels_and_the_parcels_string(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # The one real-work call (ADR 0011): it consumes the staged create+allocate
    # data, runs the atomic domain create-consignment, and returns the paperwork
    # obligations - base64 label PDF and the Parcels String (CONTEXT.md).
    _create_depot1(client)
    code = _stage_and_allocate(client, wms_auth, app)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 200
    paperwork = _paperwork_return(response.text)
    # The response is a single Paperwork return (not an array): documents first
    # (nil), then the combined label PDF, then the optional obligations.
    assert [_localname(child) for child in paperwork] == [
        "documents",
        "labels",
        "parcels",
    ]
    assert paperwork[0].get(f"{{{_XSI}}}nil") == "true"
    assert base64.b64decode(paperwork[1].text or "").startswith(b"%PDF")
    # Drop Out has no live tracking, so trackingReference is omitted.
    assert paperwork.find("trackingReference") is None
    # {order}-parcel-{n}:{barcode}, comma-joined; Drop Out reports no carrier
    # barcode, so the barcode is the Parcel Barcode (CONTEXT.md: Parcels String).
    assert _child_text(paperwork, "parcels") == (
        "95000254580-parcel-1:95000254580-1,95000254580-parcel-2:95000254580-2"
    )

    # The domain Consignment is now the system of record for the shipment.
    with app.state.session_factory() as session:
        consignment = (
            session.execute(
                select(Consignment).where(Consignment.order_number == "95000254580")
            )
            .scalars()
            .one()
        )
        assert consignment.status == "allocated"
        assert consignment.carrier == "dropout"
        # The accepted groups are persisted so a dry-run replays the filter.
        assert consignment.accepted_service_groups == ["ECONOMY"]


_FURDECO_DEFINITION = (
    Path(__file__).parent.parent / "examples" / "furdeco.definition.json"
)

_FURDECO_BOOKING_RESPONSE = (
    "<response>"
    "<success>Order Created</success>"
    "<carrier_reference>F12345678910</carrier_reference>"
    "<barcodes>001122334455667688, 123456789123456789</barcodes>"
    "</response>"
)

_FURDECO_RULEBOOK = {
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
            # Member of ECONOMY (the fixture's custom1) so the legacy filter
            # keeps it eligible (ADR 0012).
            "service_groups": ["ECONOMY"],
        }
    ],
}


def _publish_furdeco(app: FastAPI, client: TestClient) -> None:
    definition = json.loads(_FURDECO_DEFINITION.read_text())
    assert (
        client.put(
            "/api/carriers/furdeco/config",
            json={
                "api_key": "SECRET-KEY",
                "base_url": "https://api.furdeco.example/orders",
                "trading_name": "Acme Trading",
            },
        ).status_code
        == 200
    )
    version = client.post(
        "/api/carriers/furdeco/definitions/drafts",
        json={"author": "jake", "definition": definition},
    ).json()["version"]
    assert (
        client.post(
            f"/api/carriers/furdeco/definitions/versions/{version}/publish"
        ).status_code
        == 200
    )
    rulebook_version = client.post(
        "/api/rulebook/drafts", json=_FURDECO_RULEBOOK
    ).json()["version"]
    assert (
        client.post(f"/api/rulebook/versions/{rulebook_version}/publish").status_code
        == 200
    )

    def override() -> Iterator[httpx.Client]:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, text=_FURDECO_BOOKING_RESPONSE)
        )
        with httpx.Client(transport=transport) as http_client:
            yield http_client

    app.dependency_overrides[get_http_client] = override


def test_paperwork_for_a_live_carrier_carries_tracking_and_carrier_barcodes(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A live-API carrier books at paperwork: its tracking reference is present
    # (Drop Out omits it) and the parcels string carries the carrier's own
    # barcodes, not the self-generated Parcel Barcodes.
    _create_depot1(client)
    _publish_furdeco(app, client)
    code = _stage_and_allocate(client, wms_auth, app)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 200
    paperwork = _paperwork_return(response.text)
    assert [_localname(child) for child in paperwork] == [
        "documents",
        "labels",
        "trackingReference",
        "parcels",
    ]
    assert _child_text(paperwork, "trackingReference") == "F12345678910"
    assert _child_text(paperwork, "parcels") == (
        "95000254580-parcel-1:001122334455667688,"
        "95000254580-parcel-2:123456789123456789"
    )


def test_dry_run_replays_a_legacy_orders_accepted_groups(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A legacy order accepted only ECONOMY. A draft adds a cheaper NEXTDAY-only
    # service; the dry-run must exclude it (replaying the stored group filter),
    # not pick it - which it would if the accepted groups were lost on replay.
    _create_depot1(client)
    code = _stage_and_allocate(client, wms_auth, app)
    client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "DROPOUT-STD",
                "carrier": "dropout",
                "name": "Drop Out Standard",
                "weight_min_kg": "0",
                "weight_max_kg": "30",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
                "service_groups": ["ECONOMY"],
            },
            {
                "code": "DROPOUT-ND",
                "carrier": "dropout",
                "name": "Drop Out Next Day",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "1.00",
                "tie_break_order": 2,
                "service_groups": ["NEXTDAY"],
            },
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]

    response = client.post(f"/api/rulebook/versions/{version}/dry-run", json={})

    assert response.status_code == 200
    [result] = response.json()["results"]
    # The cheaper NEXTDAY service is filtered out: ECONOMY was replayed.
    assert result["draft_service"] == "DROPOUT-STD"


_PARCEL_150CM = (
    b'<parcelWeight xsi:type="xsd:double">1.3</parcelWeight>',
    b'<parcelWeight xsi:type="xsd:double">1.3</parcelWeight>'
    b'<parcelHeight xsi:type="xsd:double">150</parcelHeight>',
)


def test_dry_run_replays_a_legacy_orders_derived_max_dimension(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A legacy order with a 150cm parcel allocates against the (limit-free) demo
    # rulebook, storing max_dimension_cm=150. A draft adding a 100cm limit must
    # exclude it on replay - which needs the derived dimension persisted, not
    # re-derived optimistically as absent.
    _create_depot1(client)
    body = _fixture("create_consignments_request.xml").replace(*_PARCEL_150CM)
    client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        code = row.consignment_code
        assert isinstance(code, str)
    _allocate(client, wms_auth, code)
    client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "DROPOUT-STD",
                "carrier": "dropout",
                "name": "Drop Out Standard",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
                "service_groups": ["ECONOMY"],
                "max_dimension_cm": "100",
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]

    response = client.post(f"/api/rulebook/versions/{version}/dry-run", json={})

    assert response.status_code == 200
    [result] = response.json()["results"]
    # The 150cm dimension was replayed, so the 100cm-limited draft excludes it.
    assert result["draft_service"] is None


def test_paperwork_faults_on_a_non_finite_parcel_weight(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # Weight is required input, so a non-finite value faults (unlike an optional
    # dimension, treated as absent) rather than escaping as an uncaught 500.
    _create_depot1(client)
    body = _fixture("create_consignments_request.xml").replace(
        b'<parcelWeight xsi:type="xsd:double">1.3</parcelWeight>',
        b'<parcelWeight xsi:type="xsd:double">NaN</parcelWeight>',
    )
    client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        code = row.consignment_code
        assert isinstance(code, str)
    _allocate(client, wms_auth, code)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "is not a number" in response.text


def test_paperwork_treats_a_non_finite_parcel_dimension_as_absent(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A NaN dimension parses as a Decimal but traps on comparison; the edge must
    # treat it as absent (optimistic) and still produce paperwork, never crash
    # into an uncaught 500 outside the SOAP fault contract.
    _create_depot1(client)
    body = _fixture("create_consignments_request.xml").replace(
        b'<parcelWeight xsi:type="xsd:double">1.3</parcelWeight>',
        b'<parcelWeight xsi:type="xsd:double">1.3</parcelWeight>'
        b'<parcelHeight xsi:type="xsd:double">NaN</parcelHeight>',
    )
    client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        code = row.consignment_code
        assert isinstance(code, str)
    _allocate(client, wms_auth, code)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 200


def test_paperwork_excludes_a_service_when_a_parcel_exceeds_its_dimension_limit(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # The WMS's per-parcel dimensions derive the consignment max dimension, which
    # the dimension check enforces: a 150cm parcel exceeds the service's 100cm
    # limit, so the order has no eligible service (it would allocate if the
    # dimensions were dropped and the check ran optimistically).
    _create_depot1(client)
    limited = {
        "author": "jake",
        "services": [
            {
                "code": "DROPOUT-STD",
                "carrier": "dropout",
                "name": "Drop Out Standard",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
                "service_groups": ["ECONOMY"],
                "max_dimension_cm": "100",
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=limited).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200
    body = _fixture("create_consignments_request.xml").replace(*_PARCEL_150CM)
    client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        assert row.created_data is not None
        assert row.created_data["parcels"][0]["height_cm"] == "150"
        code = row.consignment_code
        assert isinstance(code, str)
    _allocate(client, wms_auth, code)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "could not be allocated" in response.text


def test_paperwork_finds_nothing_when_no_service_declares_a_group(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # Allow-list rollout (ADR 0012): making the group filter mandatory means a
    # rulebook whose services declare no memberships leaves a legacy order - even
    # one accepting a valid catalogue group - with no eligible service. This is
    # the intended consequence, pinned so it reads as designed, not accidental;
    # a real cutover republishes rulebooks with memberships first.
    _create_depot1(client)
    groupless = {
        "author": "jake",
        "services": [
            {
                "code": "DROPOUT-STD",
                "carrier": "dropout",
                "name": "Drop Out Standard",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=groupless).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200
    code = _stage_and_allocate(client, wms_auth, app)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "could not be allocated" in response.text


def _stage_custom1(
    client: TestClient, auth: tuple[str, str], app: FastAPI, custom1: bytes
) -> str:
    # The fixture's custom1 is ECONOMY; swap it (or remove the element with b"")
    # to drive the Service Group filter (ADR 0012).
    body = _fixture("create_consignments_request.xml").replace(
        b'<custom1 xsi:type="xsd:string">ECONOMY</custom1>', custom1
    )
    client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=auth,
    )
    with app.state.session_factory() as session:
        row = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        assert isinstance(row.consignment_code, str)
        return row.consignment_code


def _allocate(client: TestClient, auth: tuple[str, str], code: str) -> None:
    client.post(
        "/AllocationService",
        content=_allocate_body([code], []),
        headers={"Content-Type": "text/xml"},
        auth=auth,
    )


def test_paperwork_faults_on_an_unknown_service_group(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # A group the WMS knows but the catalogue does not is a sync gap; faulting
    # keeps it from silently allocating unfiltered (ADR 0012).
    _create_depot1(client)
    code = _stage_custom1(
        client, wms_auth, app, b'<custom1 xsi:type="xsd:string">MYSTERY</custom1>'
    )
    _allocate(client, wms_auth, code)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "unknown service group" in response.text
    assert "MYSTERY" in response.text


def test_paperwork_faults_when_the_order_carries_no_service_group(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # The WMS's own "no service group -> no services" behaviour, surfaced as a
    # fault rather than an unfiltered allocation (ADR 0012).
    _create_depot1(client)
    code = _stage_custom1(client, wms_auth, app, b"")
    _allocate(client, wms_auth, code)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "no service group" in response.text


def test_paperwork_filters_out_a_carrier_not_in_the_accepted_group(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # NEXTDAY is a real catalogue group, but the demo Drop Out services are only
    # in ECONOMY, so an order accepting only NEXTDAY has no eligible service.
    _create_depot1(client)
    code = _stage_custom1(
        client, wms_auth, app, b'<custom1 xsi:type="xsd:string">NEXTDAY</custom1>'
    )
    _allocate(client, wms_auth, code)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "could not be allocated" in response.text
    with app.state.session_factory() as session:
        assert not list(session.execute(select(Consignment)).scalars())


def test_paperwork_faults_with_no_codes(
    client: TestClient, wms_auth: tuple[str, str]
) -> None:
    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "no consignmentCodes" in response.text


def test_paperwork_faults_on_a_code_that_was_never_created(
    client: TestClient, wms_auth: tuple[str, str]
) -> None:
    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body(["NS9999999"]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "unknown consignmentCode" in response.text


def test_paperwork_faults_when_allocate_has_not_run(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # Strict lifecycle ordering (ADR 0011): paperwork reads the allocate call's
    # stored intent, so a code that was created but never allocated is a
    # lifecycle error, and no domain create-consignment runs for it.
    code = _stage_a_consignment(client, wms_auth, app)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "allocateConsignments must run first" in response.text
    with app.state.session_factory() as session:
        assert not list(session.execute(select(Consignment)).scalars())


def test_paperwork_faults_on_more_than_one_code(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # One shipment per call: a second code is refused up front, before any
    # domain work, so a code that books can never be stranded behind the blanket
    # fault a later code would raise (the domain commits its own failure paths).
    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body(["NS0000001", "NS0000002"]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "one consignmentCode per call" in response.text
    with app.state.session_factory() as session:
        assert not list(session.execute(select(Consignment)).scalars())


def test_paperwork_faults_on_a_blank_code(
    client: TestClient, wms_auth: tuple[str, str]
) -> None:
    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([""]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "blank consignmentCode" in response.text


def test_paperwork_faults_when_the_shipment_cannot_be_allocated(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    # No carrier serves the destination, so there is no label to return: the WMS
    # is told loudly, not handed an empty paperwork response.
    _create_depot1(client)
    body = _fixture("create_consignments_request.xml").replace(
        b'<countryCode xsi:type="xsd:string">GB</countryCode>',
        b'<countryCode xsi:type="xsd:string">US</countryCode>',
    )
    client.post(
        "/ConsignmentService",
        content=body,
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )
    with app.state.session_factory() as session:
        code = session.execute(select(LegacyConsignmentStaging)).scalars().one()
        assert isinstance(code.consignment_code, str)
        consignment_code = code.consignment_code
    client.post(
        "/AllocationService",
        content=_allocate_body([consignment_code], ["ECONOMY"]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([consignment_code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "could not be allocated" in response.text
    # The fault rolls the request back, so an unservable shipment leaves no
    # domain Consignment behind (the batch-safety rollback the edge applies to
    # every fault).
    with app.state.session_factory() as session:
        assert not list(session.execute(select(Consignment)).scalars())
