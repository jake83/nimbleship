"""The WMS-facing Legacy Interface (ADR 0002, ADR 0011): a SOAP/XML edge over
the same domain operations as the JSON API. The fixture here is a synthetic,
MetaPack-shaped request (real recorded traffic replaces it as operations land)."""

import base64
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

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
    # {order}-parcel-{n}:{Parcel Barcode}, comma-joined; n and the barcode's
    # sequence must agree (CONTEXT.md: Parcels String).
    assert (
        "95000254580-parcel-1:95000254580-1,"
        "95000254580-parcel-2:95000254580-2" in response.text
    )
    labels = re.search(r"<labels>(.*?)</labels>", response.text, re.DOTALL)
    assert labels is not None
    assert base64.b64decode(labels.group(1)).startswith(b"%PDF")

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


def test_paperwork_faults_on_a_duplicate_code_in_one_batch(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    code = _stage_and_allocate(client, wms_auth, app)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code, code]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "duplicate consignmentCode" in response.text
    # The whole batch faults before any domain work, so nothing is booked.
    with app.state.session_factory() as session:
        assert not list(session.execute(select(Consignment)).scalars())


def test_paperwork_faults_on_a_blank_code_rather_than_dropping_it(
    app: FastAPI, client: TestClient, wms_auth: tuple[str, str]
) -> None:
    _create_depot1(client)
    code = _stage_and_allocate(client, wms_auth, app)

    response = client.post(
        "/ConsignmentService",
        content=_paperwork_body([code, ""]),
        headers={"Content-Type": "text/xml"},
        auth=wms_auth,
    )

    assert response.status_code == 500
    assert "blank consignmentCode" in response.text
    # The valid code's shipment must not be half-produced when the batch faults.
    with app.state.session_factory() as session:
        assert not list(session.execute(select(Consignment)).scalars())


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
