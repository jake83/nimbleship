"""The PalletForce example definition: the multi-step rung of the Phase 3
proving ladder (ADR 0009). It must validate against the schema, render
deterministically offline (placeholder tokens for unanswered steps), and
mint its consignment number from a pre-allocated fact via the
computed-field plugin."""

import json
from pathlib import Path

from nimbleship.domain.carrier_definition import CarrierDefinition
from nimbleship.engine.render import render_operation

DEFINITION_PATH = (
    Path(__file__).parent.parent / "examples" / "palletforce.definition.json"
)


def definition() -> CarrierDefinition:
    return CarrierDefinition.model_validate(
        json.loads(DEFINITION_PATH.read_text(encoding="utf-8"))
    )


def facts(proposition: str = "next-day") -> dict[str, object]:
    return {
        "shipment": {
            "order_number": "95000254580",
            "recipient_name": "John Doe",
            "recipient_phone": "+44 20 7946 0000",
            "recipient_email": "john.doe@example.com",
            "address_lines": ["10 Downing Street", "Westminster"],
            "town": "London",
            "county": "Greater London",
            "postcode": "SW1A 2AA",
            "destination_country": "GB",
            "proposition": proposition,
            "collection_date": "20260713",
            "pallet_type": "H",
            "pallet_spaces": "1",
            "weight_kg": "410",
        },
        "config": {
            "manifest_url": "https://api.pf.example/ExternalScanning/UploadManifest",
            "label_url": "https://api.pf.example/ExternalScanning/DownloadLabel",
            "access_key": "PF-ACCESS-KEY",
            "requesting_depot": "086",
            "customer_account_number": "ACC-123",
            "insurance_code": "01",
            "hub_identifying_code": "HUB1",
            "company_name": "Acme Fulfilment Ltd",
            "company_street": "Unit 5, Trading Estate",
            "company_town": "Leicester",
            "company_county": "Leicestershire",
            "company_postcode": "LE1 1AA",
            "company_country": "GB",
            "company_phone": "+44 116 000 0000",
            "company_contact_name": "Warehouse Team",
        },
        "allocated": {"consignment_number": "42"},
    }


def test_the_example_definition_validates() -> None:
    validated = definition()

    assert validated.carrier == "palletforce"
    steps = validated.operations["book"].steps
    assert [step.name for step in steps] == ["manifest", "label"]


def test_the_manifest_step_renders_the_nested_consignment() -> None:
    manifest, _ = render_operation(definition(), "book", facts())

    assert manifest.url == "https://api.pf.example/ExternalScanning/UploadManifest"
    assert manifest.body["uniqueTransactionNumber"] == "95000254580"
    delivery = manifest.body["deliveryAddress"]
    assert isinstance(delivery, dict)
    assert delivery["streetAddress"] == "10 Downing Street, Westminster"
    assert delivery["postcode"] == "SW1A 2AA"
    consignments = manifest.body["consignments"]
    assert isinstance(consignments, list) and len(consignments) == 1
    consignment = consignments[0]
    assert isinstance(consignment, dict)
    assert consignment["pallets"] == [{"palletType": "H", "numberofPallets": "1"}]
    assert consignment["datesAndTimes"] == [
        {"dateTimeType": "COLD", "value": "20260713"}
    ]
    assert consignment["nonPalletforceConsignment"] == "N"


def test_auth_is_a_client_id_header_and_the_access_key_is_also_in_the_body() -> None:
    manifest, label = render_operation(definition(), "book", facts())

    for request in (manifest, label):
        assert request.headers == {"x-ibm-client-id": "PF-ACCESS-KEY"}
        assert request.body["accessKey"] == "PF-ACCESS-KEY"


def test_the_consignment_number_renders_from_the_allocated_fact() -> None:
    manifest, _ = render_operation(definition(), "book", facts())

    consignments = manifest.body["consignments"]
    assert isinstance(consignments, list)
    consignment = consignments[0]
    assert isinstance(consignment, dict)
    assert consignment["consignmentNumber"] == "0000042"


def test_service_and_surcharge_follow_the_proposition_lookups() -> None:
    def consignment_for(proposition: str) -> dict[str, object]:
        manifest, _ = render_operation(definition(), "book", facts(proposition))
        consignments = manifest.body["consignments"]
        assert isinstance(consignments, list)
        consignment = consignments[0]
        assert isinstance(consignment, dict)
        return consignment

    next_day = consignment_for("next-day")
    assert next_day["serviceName"] == "A"
    assert next_day["surcharges"] == "TL"

    saturday_am = consignment_for("saturday-pre-10")
    assert saturday_am["serviceName"] == "A"
    assert saturday_am["surcharges"] == "SA AM TL"

    economy = consignment_for("economy")
    assert economy["serviceName"] == "B"
    assert economy["surcharges"] == "BI TL"


def test_the_label_step_renders_a_placeholder_until_the_manifest_answers() -> None:
    _, label = render_operation(definition(), "book", facts())

    assert label.url == "https://api.pf.example/ExternalScanning/DownloadLabel"
    assert label.body["trackingNumber"] == "<steps.manifest.tracking_codes.0>"
    assert label.body["palletNumber"] == "1"
    assert label.body["uniqueTransactionNumber"] == "95000254580"


def test_the_label_step_uses_the_first_extracted_tracking_code() -> None:
    executed = {
        **facts(),
        "steps": {"manifest": {"tracking_codes": ["UMB0000042", "UMB0000043"]}},
    }

    _, label = render_operation(definition(), "book", executed)

    assert label.body["trackingNumber"] == "UMB0000042"


def test_renders_are_deterministic() -> None:
    first = render_operation(definition(), "book", facts())
    second = render_operation(definition(), "book", facts())

    assert [r.model_dump() for r in first] == [r.model_dump() for r in second]
