"""Dachser end to end against the example definition: the book operation mints
one SSCC per parcel and sends them in a REST /labels call that returns a base64
PDF; the fan-out manifest drops one ForwardingOrderInformation XML per order on
SFTP, carrying the same SSCCs. Every carrier call is a MockTransport or a
recording uploader - zero real network."""

import base64
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from nimbleship.domain.manifests import send_manifest
from nimbleship.http_client import get_http_client
from nimbleship.models import (
    CarrierConfig,
    CarrierDefinitionVersion,
    CarrierTraffic,
    Consignment,
    Manifest,
    ManifestConsignment,
    Parcel,
)

DACHSER = Path(__file__).parent.parent / "examples" / "dachser.definition.json"
FAKE_PDF = b"%PDF-1.4 a real-looking dachser label"

# A 13-digit GS1 company prefix leaves a 4-digit serial; the first two parcels
# mint serials 1 and 2, each closed by its GS1 mod-10 check digit. All fixture
# values are invented - account IDs and addresses are per-install data.
SSCC_1 = "950000000000000015"
SSCC_2 = "950000000000000022"

DACHSER_CONFIG: dict[str, object] = {
    "labels_url": "https://api.dachser.example/labels",
    "labels_api_key": "SECRET-KEY",
    "sscc_prefix": "9500000000000",
    "division": "T",
    "product": "A",
    "dispatch_branch_number": "211",
    "incoterm": "031",
    "test_flag": "1",
    "partner_id": "11111111",
    "partner_gln": "1111111111116",
    "consignor_partner_id": "22222222",
    "forwarding_gln": "3333333333338",
    "consignor_name": "Test Depot Ltd",
    "consignor_street": "1 Depot Road",
    "consignor_city": "Testville",
    "consignor_postcode": "TE1 1ST",
    "consignor_country": "GB",
    "sftp_remote_dir": "/dachser/in",
}

CONSIGNMENT = {
    "order_number": "ORD-1001",
    "recipient_name": "Test Recipient",
    "address_lines": ["1 Test Street"],
    "postcode": "1000 AA",
    "destination_country": "NL",
    "parcels": [{"weight_kg": "22.50"}, {"weight_kg": "3.10"}],
}


def _carrier_answers(app: FastAPI, text: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text)

    def override() -> Iterator[httpx.Client]:
        with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
            yield http_client

    app.dependency_overrides[get_http_client] = override


def _label_response() -> str:
    return json.dumps({"label": base64.b64encode(FAKE_PDF).decode()})


def _publish_dachser(client: TestClient) -> None:
    definition = json.loads(DACHSER.read_text())
    assert (
        client.put("/api/carriers/dachser/config", json=DACHSER_CONFIG).status_code
        == 200
    )
    version = client.post(
        "/api/carriers/dachser/definitions/drafts",
        json={"author": "jake", "definition": definition},
    ).json()["version"]
    published = client.post(
        f"/api/carriers/dachser/definitions/versions/{version}/publish"
    )
    assert published.status_code == 200, published.text
    rulebook = {
        "author": "jake",
        "services": [
            {
                "code": "DACHSER-RD",
                "carrier": "dachser",
                "name": "Dachser Road",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["NL"],
                "cost": "30.00",
                "tie_break_order": 1,
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=rulebook).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")


def test_dachser_booking_mints_ssccs_and_sends_them(
    app: FastAPI, client: TestClient
) -> None:
    _publish_dachser(client)
    _carrier_answers(app, _label_response())

    created = client.post("/api/consignments", json=CONSIGNMENT)
    assert created.status_code == 201
    assert created.json()["carrier"] == "dachser"

    detail = client.get("/api/consignments/ORD-1001").json()
    # One SSCC per parcel, from consecutive serials, stored as the carrier
    # barcode.
    assert [p["carrier_barcode"] for p in detail["parcels"]] == [SSCC_1, SSCC_2]

    with app.state.session_factory() as session:
        [row] = session.execute(select(CarrierTraffic)).scalars().all()
        body = row.request["body"]["shipment"]
        # The minted SSCCs ride the book body as a JSON array of strings.
        assert body["ssccs"] == [SSCC_1, SSCC_2]
        assert body["consignee"]["names"] == ["Test Recipient"]
        assert body["consignee"]["addressInformation"]["countryCode"] == "NL"
        assert body["references"][0] == {"code": "100", "value": "ORD-1001"}


def test_dachser_label_is_the_returned_base64_pdf(
    app: FastAPI, client: TestClient
) -> None:
    _publish_dachser(client)
    _carrier_answers(app, _label_response())

    client.post("/api/consignments", json=CONSIGNMENT)

    label = client.get("/api/consignments/ORD-1001/label.pdf")
    assert label.status_code == 200
    # The stored label is exactly the carrier's PDF, decoded from the response.
    assert label.content == FAKE_PDF


class _RecordingUploader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def upload(
        self, config: dict[str, object], remote_path: str, filename: str, content: str
    ) -> None:
        self.calls.append((remote_path, filename, content))


def _seed_dachser_manifest(app: FastAPI) -> int:
    with app.state.session_factory() as session:
        session.add(
            CarrierDefinitionVersion(
                carrier="dachser",
                version=1,
                status="published",
                author="test",
                data=json.loads(DACHSER.read_text()),
            )
        )
        session.add(CarrierConfig(carrier="dachser", data=DACHSER_CONFIG))
        consignments = []
        for order, ssccs in (
            ("ORD-1001", [SSCC_1, SSCC_2]),
            ("ORD-1002", ["950000000000000039"]),
        ):
            consignment = Consignment(
                order_number=order,
                recipient_name="Test Recipient",
                address_lines=["1 Test Street"],
                postcode="1000 AA",
                destination_country="NL",
                status="dispatched",
                carrier="dachser",
                service="DACHSER-RD",
                allocation={},
            )
            consignment.parcels = [
                Parcel(
                    sequence=i + 1,
                    weight_kg="22.50",
                    barcode=f"{order}-{i + 1}",
                    carrier_barcode=sscc,
                )
                for i, sscc in enumerate(ssccs)
            ]
            session.add(consignment)
            consignments.append(consignment)
        manifest = Manifest(carrier="dachser", status="pending")
        session.add(manifest)
        session.flush()
        for consignment in consignments:
            session.add(
                ManifestConsignment(
                    manifest_id=manifest.id, consignment_id=consignment.id
                )
            )
        session.commit()
        return manifest.id


def test_dachser_fan_out_manifest_is_one_xml_per_order_carrying_the_ssccs(
    app: FastAPI,
) -> None:
    manifest_id = _seed_dachser_manifest(app)
    uploader = _RecordingUploader()

    with (
        app.state.session_factory() as session,
        httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        ) as http_client,
    ):
        manifest = session.get(Manifest, manifest_id)
        assert manifest is not None
        send_manifest(session, manifest, http_client, {"sftp_upload": uploader})
        assert manifest.status == "sent"

    # One ForwardingOrderInformation XML per order, dropped in the configured
    # SFTP directory.
    assert [remote for remote, _, _ in uploader.calls] == ["/dachser/in", "/dachser/in"]
    assert [name for _, name, _ in uploader.calls] == ["ORD-1001.xml", "ORD-1002.xml"]

    first = uploader.calls[0][2]
    assert '<ShipmentHeader CustomerShipmentReference="ORD-1001">' in first
    assert '<ShipmentAddress AddressType="CZ">' in first
    assert '<ShipmentAddress AddressType="CN">' in first
    assert "<PartnerName>Test Recipient</PartnerName>" in first
    # The per-parcel SSCCs minted at booking reach the EDI, one
    # PackageIdentification each - the point of fanning out per order.
    assert f"<SSCCBarCode>{SSCC_1}</SSCCBarCode>" in first
    assert f"<SSCCBarCode>{SSCC_2}</SSCCBarCode>" in first
