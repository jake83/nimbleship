import io

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

CONSIGNMENT = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}, {"weight_kg": "3.1"}],
}


def test_creating_a_consignment_allocates_and_produces_a_label(
    client: TestClient,
) -> None:
    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "allocated"
    assert body["carrier"] == "dropout"
    assert body["service"] == "DROPOUT-STD"
    assert body["label_url"] == "/api/consignments/95000254580/label.pdf"
    assert body["allocation"]["reason"] == "cheapest eligible service"
    assert body["allocation"]["rulebook_version"] == 1


def test_label_pdf_is_downloadable(client: TestClient) -> None:
    client.post("/api/consignments", json=CONSIGNMENT)

    response = client.get("/api/consignments/95000254580/label.pdf")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF")


def test_timeline_records_allocation_and_label_creation(
    client: TestClient,
) -> None:
    client.post("/api/consignments", json=CONSIGNMENT)

    response = client.get("/api/consignments/95000254580")

    assert response.status_code == 200
    body = response.json()
    assert [e["stage"] for e in body["events"]] == ["allocated", "label_created"]
    assert body["parcels"] == [
        {
            "sequence": 1,
            "weight_kg": "4.2",
            "barcode": "95000254580-1",
            "carrier_barcode": None,
        },
        {
            "sequence": 2,
            "weight_kg": "3.1",
            "barcode": "95000254580-2",
            "carrier_barcode": None,
        },
    ]


def test_heavy_consignment_selects_the_larger_service(client: TestClient) -> None:
    heavy = {
        **CONSIGNMENT,
        "order_number": "95000254581",
        "parcels": [{"weight_kg": "40"}],
    }

    response = client.post("/api/consignments", json=heavy)

    assert response.status_code == 201
    assert response.json()["service"] == "DROPOUT-XL"


def test_unservable_destination_is_recorded_as_rejected(
    client: TestClient,
) -> None:
    rejected = {
        **CONSIGNMENT,
        "order_number": "95000254582",
        "destination_country": "US",
    }

    response = client.post("/api/consignments", json=rejected)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "rejected"
    assert body["service"] is None
    assert body["allocation"]["reason"] == "no eligible services"

    timeline = client.get("/api/consignments/95000254582").json()
    assert [e["stage"] for e in timeline["events"]] == ["rejected"]


def test_duplicate_order_number_conflicts(client: TestClient) -> None:
    client.post("/api/consignments", json=CONSIGNMENT)

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 409


def test_unknown_consignment_is_not_found(client: TestClient) -> None:
    assert client.get("/api/consignments/nope").status_code == 404
    assert client.get("/api/consignments/nope/label.pdf").status_code == 404


def test_non_latin_order_numbers_are_rejected_with_422(client: TestClient) -> None:
    unicode_order = {**CONSIGNMENT, "order_number": "订单1"}

    response = client.post("/api/consignments", json=unicode_order)

    assert response.status_code == 422


PROPOSITION_DRAFT = {
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
            "propositions": ["economy"],
        },
        {
            "code": "DROPOUT-XL",
            "carrier": "dropout",
            "name": "Drop Out Extra Large",
            "weight_min_kg": "0",
            "weight_max_kg": "999",
            "countries": ["GB", "IE", "FR"],
            "cost": "12.00",
            "tie_break_order": 2,
            "propositions": ["next-day"],
        },
    ],
}


def _publish_proposition_rulebook(client: TestClient) -> None:
    version = client.post("/api/rulebook/drafts", json=PROPOSITION_DRAFT).json()[
        "version"
    ]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def test_bought_proposition_filters_dispatch_to_fulfilling_services(
    client: TestClient,
) -> None:
    _publish_proposition_rulebook(client)
    next_day = {**CONSIGNMENT, "proposition": "next-day"}

    response = client.post("/api/consignments", json=next_day)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "allocated"
    # DROPOUT-STD is cheaper but only fulfils economy; the promise wins.
    assert body["service"] == "DROPOUT-XL"


def test_consignment_without_a_proposition_keeps_the_widest_offer(
    client: TestClient,
) -> None:
    _publish_proposition_rulebook(client)

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.json()["service"] == "DROPOUT-STD"


def test_dry_run_replays_the_bought_proposition(client: TestClient) -> None:
    # Allocated under the demo rulebook, where no service is restricted.
    client.post("/api/consignments", json={**CONSIGNMENT, "proposition": "next-day"})
    draft_version = client.post("/api/rulebook/drafts", json=PROPOSITION_DRAFT).json()[
        "version"
    ]

    response = client.post(f"/api/rulebook/versions/{draft_version}/dry-run", json={})

    [result] = response.json()["results"]
    # The replay must honour the promise the customer bought: under the
    # draft only DROPOUT-XL fulfils next-day.
    assert result["draft_service"] == "DROPOUT-XL"
    assert result["changed"] is True


WAREHOUSE = {
    "code": "MAIN",
    "name": "Main Warehouse",
    "company_name": "Acme Fulfilment Ltd",
    "address_lines": ["Unit 5, Trading Estate"],
    "postcode": "LE1 1AA",
    "country": "GB",
}


def _label_text(client: TestClient, order_number: str) -> str:
    pdf = client.get(f"/api/consignments/{order_number}/label.pdf").content
    return PdfReader(io.BytesIO(pdf)).pages[0].extract_text()


def test_consignment_stores_its_warehouse_and_labels_carry_its_sender_details(
    client: TestClient,
) -> None:
    client.post("/api/warehouses", json=WAREHOUSE)

    response = client.post(
        "/api/consignments", json={**CONSIGNMENT, "warehouse": "MAIN"}
    )

    assert response.status_code == 201
    assert response.json()["warehouse"] == "MAIN"
    assert client.get("/api/consignments/95000254580").json()["warehouse"] == "MAIN"
    text = _label_text(client, "95000254580")
    assert "Acme Fulfilment Ltd" in text
    assert "LE1 1AA" in text


def test_consignment_without_a_warehouse_has_no_sender_details(
    client: TestClient,
) -> None:
    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    assert response.json()["warehouse"] is None
    assert "From" not in _label_text(client, "95000254580")


def test_unknown_warehouse_code_is_rejected_and_stores_nothing(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/consignments", json={**CONSIGNMENT, "warehouse": "NOPE"}
    )

    assert response.status_code == 422
    assert client.get("/api/consignments/95000254580").status_code == 404


def test_losing_a_duplicate_race_still_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nimbleship.routers.consignments as consignments_module

    client.post("/api/consignments", json=CONSIGNMENT)
    # Simulate the race: the existence pre-check misses the row that another
    # request has already committed, so the unique constraint is the last line
    # of defence.
    monkeypatch.setattr(
        consignments_module, "_order_exists", lambda session, order_number: False
    )

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 409


def _publish_services(client: TestClient, services: list[dict[str, object]]) -> None:
    draft = client.post("/api/rulebook/drafts", json={"services": services})
    assert draft.status_code == 201
    version = draft.json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def test_destination_in_a_blocked_area_excludes_the_blocking_service(
    client: TestClient,
) -> None:
    client.post(
        "/api/shipping-areas",
        json={
            "code": "HIGHLANDS",
            "name": "Scottish Highlands",
            "country": "GB",
            "prefixes": ["IV"],
        },
    )
    base = {
        "carrier": "dropout",
        "weight_min_kg": "0",
        "weight_max_kg": "999",
        "countries": ["GB"],
    }
    _publish_services(
        client,
        [
            {
                **base,
                "code": "CHEAP-MAINLAND",
                "name": "Cheap Mainland",
                "cost": "4.50",
                "tie_break_order": 1,
                "areas_blocked": ["HIGHLANDS"],
            },
            {
                **base,
                "code": "EVERYWHERE",
                "name": "Everywhere",
                "cost": "12.00",
                "tie_break_order": 2,
            },
        ],
    )
    highlands_order = {
        **CONSIGNMENT,
        "order_number": "95000254590",
        "postcode": "IV1 2AB",
    }

    response = client.post("/api/consignments", json=highlands_order)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "allocated"
    assert body["service"] == "EVERYWHERE"
    blocked = next(
        r
        for r in body["allocation"]["service_results"]
        if r["service_code"] == "CHEAP-MAINLAND"
    )
    assert blocked["eligible"] is False
    failed = [c["name"] for c in blocked["checks"] if not c["ok"]]
    assert failed == ["area_blocked"]


def test_destination_outside_the_blocked_area_keeps_the_cheap_service(
    client: TestClient,
) -> None:
    client.post(
        "/api/shipping-areas",
        json={
            "code": "HIGHLANDS",
            "name": "Scottish Highlands",
            "country": "GB",
            "prefixes": ["IV"],
        },
    )
    base = {
        "carrier": "dropout",
        "weight_min_kg": "0",
        "weight_max_kg": "999",
        "countries": ["GB"],
    }
    _publish_services(
        client,
        [
            {
                **base,
                "code": "CHEAP-MAINLAND",
                "name": "Cheap Mainland",
                "cost": "4.50",
                "tie_break_order": 1,
                "areas_blocked": ["HIGHLANDS"],
            },
            {
                **base,
                "code": "EVERYWHERE",
                "name": "Everywhere",
                "cost": "12.00",
                "tie_break_order": 2,
            },
        ],
    )

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    assert response.json()["service"] == "CHEAP-MAINLAND"


def test_force_service_is_refused_when_testing_tools_are_disabled(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/consignments",
        json={**CONSIGNMENT, "force_service": "DROPOUT-XL"},
    )

    assert response.status_code == 403


def test_force_service_pins_the_allocation_when_testing_tools_are_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIMBLESHIP_TESTING_TOOLS_ENABLED", "true")

    response = client.post(
        "/api/consignments",
        json={**CONSIGNMENT, "force_service": "DROPOUT-XL"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "allocated"
    assert body["service"] == "DROPOUT-XL"
    assert body["allocation"]["reason"] == "forced by testing tools"

    timeline = client.get(f"/api/consignments/{CONSIGNMENT['order_number']}").json()
    allocated = next(e for e in timeline["events"] if e["stage"] == "allocated")
    assert allocated["detail"]["forced"] is True


def test_forcing_an_unknown_service_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIMBLESHIP_TESTING_TOOLS_ENABLED", "true")

    response = client.post(
        "/api/consignments",
        json={**CONSIGNMENT, "force_service": "NOPE"},
    )

    assert response.status_code == 422


def test_forced_allocation_records_a_real_cost_not_a_sentinel(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIMBLESHIP_TESTING_TOOLS_ENABLED", "true")

    client.post(
        "/api/consignments",
        json={**CONSIGNMENT, "force_service": "DROPOUT-XL"},
    )

    timeline = client.get(f"/api/consignments/{CONSIGNMENT['order_number']}").json()
    allocated = next(e for e in timeline["events"] if e["stage"] == "allocated")
    assert allocated["detail"]["cost"] == "12.00"
    assert timeline["allocation"]["selected_cost"] == "12.00"


def test_forcing_a_costless_service_records_an_honest_null_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIMBLESHIP_TESTING_TOOLS_ENABLED", "true")
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "COSTLESS",
                "carrier": "dropout",
                "name": "Bands that cover nothing",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
                "cost_bands": [
                    {
                        "cost_type": "consignment_weight",
                        "min_weight_kg": "0",
                        "max_weight_kg": "1",
                        "charge": "2.00",
                    }
                ],
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200

    response = client.post(
        "/api/consignments", json={**CONSIGNMENT, "force_service": "COSTLESS"}
    )

    assert response.status_code == 201
    timeline = client.get(f"/api/consignments/{CONSIGNMENT['order_number']}").json()
    allocated = next(e for e in timeline["events"] if e["stage"] == "allocated")
    assert allocated["detail"]["cost"] is None
    assert allocated["detail"]["cost"] != "None"
    assert timeline["allocation"]["selected_cost"] is None


def test_forcing_a_banded_service_records_its_banded_cost(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NIMBLESHIP_TESTING_TOOLS_ENABLED", "true")
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "BANDED",
                "carrier": "dropout",
                "name": "Banded",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "99.00",
                "tie_break_order": 1,
                "cost_bands": [
                    {
                        "cost_type": "consignment_weight",
                        "min_weight_kg": "0",
                        "max_weight_kg": "999",
                        "charge": "3.25",
                    }
                ],
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200

    client.post("/api/consignments", json={**CONSIGNMENT, "force_service": "BANDED"})

    timeline = client.get(f"/api/consignments/{CONSIGNMENT['order_number']}").json()
    allocated = next(e for e in timeline["events"] if e["stage"] == "allocated")
    assert allocated["detail"]["cost"] == "3.25"


def test_labels_flow_through_the_carrier_definition(client: TestClient) -> None:
    """The walking skeleton's hardcoded Drop Out path is gone: the label is
    produced because the dropout definition's book operation declares
    local_render."""
    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 201
    label = client.get("/api/consignments/95000254580/label.pdf")
    assert label.status_code == 200
    assert label.content.startswith(b"%PDF")


def test_a_carrier_without_a_published_definition_fails_loudly(
    client: TestClient,
) -> None:
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "GHOST-STD",
                "carrier": "ghostcarrier",
                "name": "No definition exists for this carrier",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 500
    assert "ghostcarrier" in response.text
    assert "definition" in response.text
