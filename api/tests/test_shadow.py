"""Shadow mode allocation-diff (ADR 0015): a golden recording of an order's
create+allocate SOAP plus the incumbent's outcome is replayed through the real
legacy edge, side-effect-free, and NimbleShip's allocation is diffed against it."""

import json
from collections.abc import Mapping
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.labels.store import LabelStore
from nimbleship.models import (
    CarrierNumberSequence,
    CarrierTraffic,
    Consignment,
    LegacyConsignmentStaging,
)
from nimbleship.shadow import (
    AllocationOutcome,
    CarrierBookResponse,
    GoldenRecording,
    replay_all,
    replay_allocation,
    replay_paperwork,
)
from nimbleship.uploaders import FileUploader

_FIXTURES = Path(__file__).parent / "fixtures" / "metapack"
# The incumbent's server-minted code that its recorded allocate SOAP references;
# NimbleShip mints its own, so the replay maps this to it.
_INCUMBENT_CODE = "META-000999"


def _fixture(name: str) -> bytes:
    return (_FIXTURES / name).read_bytes()


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


def _seed_warehouse(client: TestClient) -> None:
    # The fixture order's senderCode; allocate_only resolves it or faults.
    assert (
        client.post(
            "/api/warehouses",
            json={
                "code": "DEPOT1",
                "name": "Depot 1",
                "address_lines": ["1 Dock Road"],
                "postcode": "M1 1AA",
                "country": "GB",
                "timezone": "Europe/London",
            },
        ).status_code
        == 201
    )


def _publish_econ_rulebook(client: TestClient, weight_min: str = "0") -> None:
    # A GB ECONOMY service; weight_min above the order's 2.5kg makes it ineligible.
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "DROPOUT-STD",
                "carrier": "dropout",
                "name": "Drop Out Standard",
                "weight_min_kg": weight_min,
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
                "service_groups": ["ECONOMY"],
            },
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def _seed_config(client: TestClient) -> None:
    _seed_warehouse(client)
    _publish_econ_rulebook(client)


def _recording(incumbent: AllocationOutcome) -> GoldenRecording:
    return GoldenRecording(
        order_number="95000254580",
        create_consignments=_fixture("create_consignments_request.xml"),
        incumbent_code=_INCUMBENT_CODE,
        allocate_consignments=_allocate_body([_INCUMBENT_CODE], ["ECONOMY"]),
        incumbent=incumbent,
    )


def _deps(
    tmp_path: Path,
) -> tuple[LabelStore, httpx.Client, Mapping[str, FileUploader]]:
    # store/http/uploaders are the edge's paperwork deps, inert for create+allocate;
    # the http client raises if the replay ever tries a carrier call it must not.
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("shadow replay must not make a carrier call")

    uploaders: Mapping[str, FileUploader] = {}
    return (
        LabelStore(tmp_path / "labels"),
        httpx.Client(transport=httpx.MockTransport(refuse)),
        uploaders,
    )


def test_a_matching_allocation_is_no_divergence(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    _seed_config(client)
    store, http_client, uploaders = _deps(tmp_path)
    recording = _recording(
        AllocationOutcome(allocated=True, carrier="dropout", service="DROPOUT-STD")
    )

    with app.state.session_factory() as session:
        diff = replay_allocation(session, recording, store, http_client, uploaders)

    assert diff.nimbleship == AllocationOutcome(
        allocated=True, carrier="dropout", service="DROPOUT-STD"
    )
    assert diff.matched


def test_a_different_carrier_choice_is_a_divergence(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    _seed_config(client)
    store, http_client, uploaders = _deps(tmp_path)
    # The incumbent claims a next-day service NimbleShip's rulebook wouldn't pick
    # for an ECONOMY order - a divergence to review.
    recording = _recording(
        AllocationOutcome(allocated=True, carrier="dropout", service="DROPOUT-ND")
    )

    with app.state.session_factory() as session:
        report = replay_all(session, [recording], store, http_client, uploaders)

    assert report.matched == 0
    [divergence] = report.divergences
    assert divergence.nimbleship.service == "DROPOUT-STD"
    assert divergence.incumbent.service == "DROPOUT-ND"


def test_replay_leaves_no_trace(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    # Side-effect-free: the replay stages and allocates in a rolled-back savepoint,
    # so no consignment or staging row survives.
    _seed_config(client)
    store, http_client, uploaders = _deps(tmp_path)
    recording = _recording(
        AllocationOutcome(allocated=True, carrier="dropout", service="DROPOUT-STD")
    )

    with app.state.session_factory() as session:
        replay_allocation(session, recording, store, http_client, uploaders)

    with app.state.session_factory() as session:
        assert session.execute(select(Consignment)).scalars().all() == []
        assert session.execute(select(LegacyConsignmentStaging)).scalars().all() == []


def test_a_clean_rejection_where_the_incumbent_allocated_is_a_divergence(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    # No service fits the order (the only service's weight band excludes it), so
    # NimbleShip rejects with no error - a divergence from an allocating incumbent.
    _seed_warehouse(client)
    _publish_econ_rulebook(client, weight_min="100")
    store, http_client, uploaders = _deps(tmp_path)
    recording = _recording(
        AllocationOutcome(allocated=True, carrier="dropout", service="DROPOUT-STD")
    )

    with app.state.session_factory() as session:
        diff = replay_allocation(session, recording, store, http_client, uploaders)

    assert diff.nimbleship == AllocationOutcome(allocated=False)
    assert not diff.matched


def test_a_fault_where_the_incumbent_allocated_is_a_divergence_carrying_the_error(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    # NimbleShip faulting (here: the order's warehouse is not seeded) where the
    # incumbent allocated is a divergence, not a harness crash; the error is kept.
    store, http_client, uploaders = _deps(tmp_path)
    recording = _recording(
        AllocationOutcome(allocated=True, carrier="dropout", service="DROPOUT-STD")
    )

    with app.state.session_factory() as session:
        diff = replay_allocation(session, recording, store, http_client, uploaders)

    assert diff.nimbleship.allocated is False
    assert diff.nimbleship.error is not None
    assert "warehouse" in diff.nimbleship.error
    assert not diff.matched


def test_a_mismatched_recording_is_isolated_not_a_batch_crash(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    # A recording whose order_number disagrees with its own create payload is a
    # capture glitch; it must be flagged, not abort the whole batch report.
    _seed_config(client)
    store, http_client, uploaders = _deps(tmp_path)
    bad = GoldenRecording(
        order_number="NOT-THE-STAGED-ORDER",  # its payload stages 95000254580
        create_consignments=_fixture("create_consignments_request.xml"),
        incumbent_code=_INCUMBENT_CODE,
        allocate_consignments=_allocate_body([_INCUMBENT_CODE], ["ECONOMY"]),
        incumbent=AllocationOutcome(allocated=True, carrier="dropout"),
    )
    good = _recording(
        AllocationOutcome(allocated=True, carrier="dropout", service="DROPOUT-STD")
    )

    with app.state.session_factory() as session:
        report = replay_all(session, [bad, good], store, http_client, uploaders)

    assert len(report.diffs) == 2  # the batch completed despite the bad recording
    [bad_diff] = [d for d in report.diffs if d.order_number == "NOT-THE-STAGED-ORDER"]
    assert bad_diff.nimbleship.error is not None
    assert not bad_diff.matched
    [good_diff] = [d for d in report.diffs if d.order_number == "95000254580"]
    assert good_diff.matched  # the valid recording still processed


def test_both_declining_with_differing_error_text_is_a_match(
    app: FastAPI, client: TestClient, tmp_path: Path
) -> None:
    # When both systems decline, differing diagnostic error text (WMS-native vs
    # ours) must not read as a divergence - only the decision is diffed.
    _seed_warehouse(client)
    _publish_econ_rulebook(client, weight_min="100")  # no service fits -> declined
    store, http_client, uploaders = _deps(tmp_path)
    recording = _recording(
        AllocationOutcome(allocated=False, error="incumbent: no eligible carrier")
    )

    with app.state.session_factory() as session:
        diff = replay_allocation(session, recording, store, http_client, uploaders)

    assert diff.nimbleship == AllocationOutcome(allocated=False)  # clean decline
    assert diff.incumbent.error != diff.nimbleship.error
    assert diff.matched


_INCUMBENT_PARCELS = (
    "95000254580-parcel-1:95000254580-1,95000254580-parcel-2:95000254580-2"
)


def _paperwork_recording(incumbent_parcels: str | None) -> GoldenRecording:
    return GoldenRecording(
        order_number="95000254580",
        create_consignments=_fixture("create_consignments_request.xml"),
        incumbent_code=_INCUMBENT_CODE,
        allocate_consignments=_allocate_body([_INCUMBENT_CODE], ["ECONOMY"]),
        incumbent=AllocationOutcome(allocated=True, carrier="dropout"),
        incumbent_parcels_string=incumbent_parcels,
    )


def test_a_matching_parcels_string_with_a_label_is_no_divergence(
    app: FastAPI, client: TestClient
) -> None:
    # A local-render (dropout) order: NimbleShip produces a real label and a
    # Parcels String matching the incumbent's - no divergence.
    _seed_config(client)
    recording = _paperwork_recording(_INCUMBENT_PARCELS)

    with app.state.session_factory() as session:
        diff = replay_paperwork(session, recording)

    assert diff.nimbleship.label_produced
    assert diff.nimbleship.parcels_string == _INCUMBENT_PARCELS
    assert diff.matched


def test_a_differing_parcels_string_is_a_divergence(
    app: FastAPI, client: TestClient
) -> None:
    _seed_config(client)
    recording = _paperwork_recording("95000254580-parcel-1:A-WRONG-BARCODE")

    with app.state.session_factory() as session:
        diff = replay_paperwork(session, recording)

    assert diff.nimbleship.parcels_string == _INCUMBENT_PARCELS  # NimbleShip's real one
    assert diff.nimbleship.label_produced
    assert not diff.matched


def test_paperwork_replay_leaves_no_trace(app: FastAPI, client: TestClient) -> None:
    # In-memory label store + savepoint: no consignment or staging row survives,
    # and nothing is written to the label store on disk.
    _seed_config(client)
    recording = _paperwork_recording(_INCUMBENT_PARCELS)

    with app.state.session_factory() as session:
        replay_paperwork(session, recording)

    with app.state.session_factory() as session:
        assert session.execute(select(Consignment)).scalars().all() == []
        assert session.execute(select(LegacyConsignmentStaging)).scalars().all() == []


# A carrier that books via an SSCC mint (a separate committing session) plus an
# http step - the paperwork slice must refuse it, never book it.
_SSCC_DEFINITION = {
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
                            {"target": "order", "source": "shipment.order_number"}
                        ],
                    },
                    "response": {
                        "format": "json",
                        "success_when": {"path": "label_pdf"},
                        "extract": [{"name": "label_pdf", "path": "label_pdf"}],
                    },
                }
            ],
            "label": {"source": "base64_pdf", "from_extract": "label_pdf"},
        }
    },
}


def _publish_sscc_econ_carrier(client: TestClient) -> None:
    # Publish ssccarrier as the sole ECONOMY-group service so the fixture order's
    # ECONOMY-filtered allocate selects it - the booking carrier the guard refuses.
    client.put(
        "/api/carriers/ssccarrier/config",
        json={
            "labels_url": "https://api.ssc.example/labels",
            "sscc_prefix": "0012345678",
        },
    )
    version = client.post(
        "/api/carriers/ssccarrier/definitions/drafts",
        json={"author": "jake", "definition": _SSCC_DEFINITION},
    ).json()["version"]
    published = client.post(
        f"/api/carriers/ssccarrier/definitions/versions/{version}/publish"
    )
    assert published.status_code == 200, published.text
    _seed_warehouse(client)
    draft = {
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
                "service_groups": ["ECONOMY"],
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def test_a_booking_carrier_is_a_divergence_not_a_leak(
    app: FastAPI, client: TestClient
) -> None:
    # A booking carrier (SSCC mint + carrier call) selected by a replayed order:
    # the slice must refuse it and mint nothing, since those escape the savepoint.
    _publish_sscc_econ_carrier(client)
    recording = _paperwork_recording(_INCUMBENT_PARCELS)

    with app.state.session_factory() as session:
        diff = replay_paperwork(session, recording)

    assert not diff.matched
    assert not diff.nimbleship.label_produced
    assert diff.nimbleship.error is not None
    assert "ssccarrier" in diff.nimbleship.error

    with app.state.session_factory() as session:
        assert session.execute(select(CarrierNumberSequence)).scalars().all() == []
        assert session.execute(select(Consignment)).scalars().all() == []
        assert session.execute(select(CarrierTraffic)).scalars().all() == []


def test_a_consignment_error_from_the_edge_is_a_divergence(
    app: FastAPI, client: TestClient
) -> None:
    # A consignment already exists for the order, so the replay's create_consignment
    # raises the 409 the edge wraps as a SoapFault; replay_paperwork must isolate it
    # as a divergence, pinning its ConsignmentError/SoapFault branch.
    _seed_config(client)
    created = client.post(
        "/api/consignments",
        json={
            "order_number": "95000254580",
            "recipient_name": "John Doe",
            "address_lines": ["10 Downing Street", "London"],
            "postcode": "SW1A 2AA",
            "destination_country": "GB",
            "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],
        },
    )
    assert created.status_code == 201, created.text
    recording = _paperwork_recording(_INCUMBENT_PARCELS)

    with app.state.session_factory() as session:
        diff = replay_paperwork(session, recording)

    assert not diff.matched
    assert not diff.nimbleship.label_produced
    assert diff.nimbleship.error is not None
    assert "already exists" in diff.nimbleship.error


def test_paperwork_no_staged_consignment_is_isolated(
    app: FastAPI, client: TestClient
) -> None:
    # A recording whose order_number disagrees with its create payload cannot find
    # a staged code; it is flagged as a bad recording, not a crash - the paperwork
    # mirror of the allocation slice's capture-glitch handling.
    _seed_config(client)
    bad = GoldenRecording(
        order_number="NOT-THE-STAGED-ORDER",  # its payload stages 95000254580
        create_consignments=_fixture("create_consignments_request.xml"),
        incumbent_code=_INCUMBENT_CODE,
        allocate_consignments=_allocate_body([_INCUMBENT_CODE], ["ECONOMY"]),
        incumbent=AllocationOutcome(allocated=True, carrier="dropout"),
        incumbent_parcels_string=_INCUMBENT_PARCELS,
    )

    with app.state.session_factory() as session:
        diff = replay_paperwork(session, bad)

    assert not diff.matched
    assert diff.nimbleship.error is not None
    assert "no staged consignment" in diff.nimbleship.error


# --- Live-API carrier slice, rung 1: Furdeco (http-book, local-render, no SSCC).
_FURDECO_EXAMPLE = Path(__file__).parent.parent / "examples" / "furdeco.definition.json"
_FURDECO_BOOK_RESPONSE = (
    "<response>"
    "<success>Order Created</success>"
    "<carrier_reference>F12345678910</carrier_reference>"
    "<barcodes>001122334455667688, 123456789123456789</barcodes>"
    "</response>"
)
_FURDECO_TRACKING = "F12345678910"
_FURDECO_PARCELS = (
    "95000254580-parcel-1:001122334455667688,95000254580-parcel-2:123456789123456789"
)


def _publish_furdeco_econ(client: TestClient) -> None:
    # Furdeco as the sole ECONOMY-group service, so the fixture order's
    # ECONOMY-filtered allocate selects it - a real booking carrier the slice
    # replays (http call, local-render label, no client-side mint).
    _seed_warehouse(client)
    client.put(
        "/api/carriers/furdeco/config",
        json={
            "api_key": "SECRET-KEY",
            "base_url": "https://api.furdeco.example/orders",
            "trading_name": "Acme Trading",
        },
    )
    definition = json.loads(_FURDECO_EXAMPLE.read_text())
    version = client.post(
        "/api/carriers/furdeco/definitions/drafts",
        json={"author": "jake", "definition": definition},
    ).json()["version"]
    published = client.post(
        f"/api/carriers/furdeco/definitions/versions/{version}/publish"
    )
    assert published.status_code == 200, published.text
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "FURDECO-STD",
                "carrier": "furdeco",
                "name": "Furdeco Std",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "5.00",
                "tie_break_order": 1,
                "service_groups": ["ECONOMY"],
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def _furdeco_recording(tracking: str | None, parcels: str | None) -> GoldenRecording:
    return GoldenRecording(
        order_number="95000254580",
        create_consignments=_fixture("create_consignments_request.xml"),
        incumbent_code=_INCUMBENT_CODE,
        allocate_consignments=_allocate_body([_INCUMBENT_CODE], ["ECONOMY"]),
        incumbent=AllocationOutcome(allocated=True, carrier="furdeco"),
        incumbent_parcels_string=parcels,
        incumbent_tracking_reference=tracking,
        carrier_book_response=CarrierBookResponse(
            status=200, body=_FURDECO_BOOK_RESPONSE
        ),
    )


def test_a_matching_http_book_carrier_is_no_divergence(
    app: FastAPI, client: TestClient
) -> None:
    # A booking carrier's recorded response replays through the real book step:
    # NimbleShip's parsed tracking reference and carrier barcodes match, it renders
    # a label, and no CarrierTraffic escapes the savepoint (in-memory sink).
    _publish_furdeco_econ(client)
    recording = _furdeco_recording(_FURDECO_TRACKING, _FURDECO_PARCELS)

    with app.state.session_factory() as session:
        diff = replay_paperwork(session, recording)

    assert diff.nimbleship.error is None
    assert diff.nimbleship.label_produced
    assert diff.nimbleship.parcels_string == _FURDECO_PARCELS
    assert diff.nimbleship.tracking_reference == _FURDECO_TRACKING
    assert diff.matched

    with app.state.session_factory() as session:
        assert session.execute(select(CarrierTraffic)).scalars().all() == []
        assert session.execute(select(Consignment)).scalars().all() == []


def test_a_differing_tracking_reference_is_a_divergence(
    app: FastAPI, client: TestClient
) -> None:
    # The tracking reference is its own diff dimension: a byte-perfect everything
    # else must still diverge when NimbleShip's extracted reference disagrees.
    _publish_furdeco_econ(client)
    recording = _furdeco_recording("A-DIFFERENT-REF", _FURDECO_PARCELS)

    with app.state.session_factory() as session:
        diff = replay_paperwork(session, recording)

    assert diff.nimbleship.tracking_reference == _FURDECO_TRACKING  # NimbleShip's real
    assert diff.nimbleship.parcels_string == _FURDECO_PARCELS
    assert not diff.matched
