"""Shadow mode allocation-diff (ADR 0015): a golden recording of an order's
create+allocate SOAP plus the incumbent's outcome is replayed through the real
legacy edge, side-effect-free, and NimbleShip's allocation is diffed against it."""

from collections.abc import Mapping
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.labels.store import LabelStore
from nimbleship.models import Consignment, LegacyConsignmentStaging
from nimbleship.shadow import (
    AllocationOutcome,
    GoldenRecording,
    replay_all,
    replay_allocation,
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
