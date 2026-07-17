from decimal import Decimal

from fastapi.testclient import TestClient

GB_CONSIGNMENT = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}],
}

US_CONSIGNMENT = {
    "order_number": "95000254581",
    "recipient_name": "Jane Doe",
    "address_lines": ["1600 Pennsylvania Ave", "Washington"],
    "postcode": "20500",
    "destination_country": "US",
    "parcels": [{"weight_kg": "4.2"}],
}

DRAFT_WITH_US = {
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
        },
        {
            "code": "DROPOUT-XL",
            "carrier": "dropout",
            "name": "Drop Out Extra Large",
            "weight_min_kg": "0",
            "weight_max_kg": "999",
            "countries": ["GB", "IE", "FR", "US"],
            "cost": "12.00",
            "tie_break_order": 2,
        },
    ],
}


def test_active_rulebook_seeds_and_reports_version_one(client: TestClient) -> None:
    response = client.get("/api/rulebook/active")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert [s["code"] for s in body["services"]] == ["DROPOUT-STD", "DROPOUT-XL"]


def test_versions_lists_seed_and_new_draft(client: TestClient) -> None:
    client.get("/api/rulebook/active")

    created = client.post("/api/rulebook/drafts", json=DRAFT_WITH_US)
    assert created.status_code == 201
    assert created.json() == {"version": 2, "status": "draft", "author": "jake"}

    versions = client.get("/api/rulebook/versions").json()
    assert [(v["version"], v["status"]) for v in versions] == [
        (1, "published"),
        (2, "draft"),
    ]


def test_get_single_version_returns_metadata_and_services(
    client: TestClient,
) -> None:
    client.get("/api/rulebook/active")
    client.post("/api/rulebook/drafts", json=DRAFT_WITH_US)

    response = client.get("/api/rulebook/versions/2")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 2
    assert body["status"] == "draft"
    assert body["author"] == "jake"
    assert "created_at" in body
    assert [s["code"] for s in body["services"]] == ["DROPOUT-STD", "DROPOUT-XL"]
    assert body["services"][1]["countries"] == ["GB", "IE", "FR", "US"]


def test_get_unknown_version_is_not_found(client: TestClient) -> None:
    assert client.get("/api/rulebook/versions/99").status_code == 404


def test_invalid_draft_is_rejected(client: TestClient) -> None:
    bad = {
        "author": "jake",
        "services": [
            DRAFT_WITH_US["services"][0],
            {**DRAFT_WITH_US["services"][1], "code": "DROPOUT-STD"},  # type: ignore[dict-item]
        ],
    }

    response = client.post("/api/rulebook/drafts", json=bad)

    assert response.status_code == 422
    assert "duplicate service code" in response.text


def test_draft_does_not_affect_live_allocation(client: TestClient) -> None:
    client.get("/api/rulebook/active")
    client.post("/api/rulebook/drafts", json=DRAFT_WITH_US)

    response = client.post("/api/consignments", json=US_CONSIGNMENT)

    assert response.json()["status"] == "rejected"


def test_dry_run_reports_what_a_draft_would_change(client: TestClient) -> None:
    client.post("/api/consignments", json=GB_CONSIGNMENT)
    client.post("/api/consignments", json=US_CONSIGNMENT)
    draft_version = client.post("/api/rulebook/drafts", json=DRAFT_WITH_US).json()[
        "version"
    ]

    response = client.post(f"/api/rulebook/versions/{draft_version}/dry-run", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["rulebook_version"] == draft_version
    assert body["total"] == 2
    assert body["changed"] == 1
    by_order = {r["order_number"]: r for r in body["results"]}
    assert by_order["95000254580"] == {
        "order_number": "95000254580",
        "current_service": "DROPOUT-STD",
        "draft_service": "DROPOUT-STD",
        "changed": False,
    }
    assert by_order["95000254581"] == {
        "order_number": "95000254581",
        "current_service": None,
        "draft_service": "DROPOUT-XL",
        "changed": True,
    }


def test_dry_run_can_target_specific_orders(client: TestClient) -> None:
    client.post("/api/consignments", json=GB_CONSIGNMENT)
    client.post("/api/consignments", json=US_CONSIGNMENT)
    draft_version = client.post("/api/rulebook/drafts", json=DRAFT_WITH_US).json()[
        "version"
    ]

    response = client.post(
        f"/api/rulebook/versions/{draft_version}/dry-run",
        json={"order_numbers": ["95000254581"]},
    )

    body = response.json()
    assert body["total"] == 1
    assert body["results"][0]["order_number"] == "95000254581"


def test_dry_run_unknown_version_is_not_found(client: TestClient) -> None:
    assert client.post("/api/rulebook/versions/99/dry-run", json={}).status_code == 404


def test_publishing_a_draft_changes_live_allocation(client: TestClient) -> None:
    rejected = client.post("/api/consignments", json=US_CONSIGNMENT)
    assert rejected.json()["status"] == "rejected"
    draft_version = client.post("/api/rulebook/drafts", json=DRAFT_WITH_US).json()[
        "version"
    ]

    published = client.post(f"/api/rulebook/versions/{draft_version}/publish")

    assert published.status_code == 200
    assert published.json()["status"] == "published"
    assert client.get("/api/rulebook/active").json()["version"] == draft_version

    retry = client.post(
        "/api/consignments", json={**US_CONSIGNMENT, "order_number": "95000254582"}
    )
    assert retry.json()["status"] == "dispatched"
    assert retry.json()["service"] == "DROPOUT-XL"


def test_publishing_an_already_published_version_conflicts(
    client: TestClient,
) -> None:
    client.get("/api/rulebook/active")

    response = client.post("/api/rulebook/versions/1/publish")

    assert response.status_code == 409


def test_publishing_unknown_version_is_not_found(client: TestClient) -> None:
    assert client.post("/api/rulebook/versions/99/publish").status_code == 404


def test_publishing_a_draft_older_than_the_live_version_conflicts(
    client: TestClient,
) -> None:
    client.get("/api/rulebook/active")
    older = client.post("/api/rulebook/drafts", json=DRAFT_WITH_US).json()["version"]
    newer = client.post("/api/rulebook/drafts", json=DRAFT_WITH_US).json()["version"]
    client.post(f"/api/rulebook/versions/{newer}/publish")

    response = client.post(f"/api/rulebook/versions/{older}/publish")

    assert response.status_code == 409
    assert "would not become live" in response.text


def test_overlong_author_is_rejected_not_a_server_error(client: TestClient) -> None:
    draft = {**DRAFT_WITH_US, "author": "x" * 65}

    response = client.post("/api/rulebook/drafts", json=draft)

    assert response.status_code == 422


def test_an_empty_draft_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/rulebook/drafts", json={"author": "ops", "services": []}
    )

    assert response.status_code == 422


def _draft_with_propositions(codes: list[str]) -> dict[str, object]:
    services = [
        {**DRAFT_WITH_US["services"][0], "propositions": codes},  # type: ignore[dict-item]
        DRAFT_WITH_US["services"][1],
    ]
    return {"author": "jake", "services": services}


def test_draft_naming_catalogue_propositions_is_accepted(client: TestClient) -> None:
    response = client.post(
        "/api/rulebook/drafts", json=_draft_with_propositions(["next-day", "economy"])
    )

    assert response.status_code == 201


def test_draft_naming_an_unknown_proposition_is_rejected_at_draft_time(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/rulebook/drafts", json=_draft_with_propositions(["same-day"])
    )

    assert response.status_code == 422
    assert "same-day" in response.text
    versions = client.get("/api/rulebook/versions").json()
    assert [v["status"] for v in versions] == ["published"]


def test_draft_may_name_a_proposition_added_to_the_catalogue(
    client: TestClient,
) -> None:
    client.post(
        "/api/propositions",
        json={"code": "saturday", "name": "Saturday", "description": ""},
    )

    response = client.post(
        "/api/rulebook/drafts", json=_draft_with_propositions(["saturday"])
    )

    assert response.status_code == 201


def _draft_with_service_groups(codes: list[str]) -> dict[str, object]:
    services = [
        {**DRAFT_WITH_US["services"][0], "service_groups": codes},  # type: ignore[dict-item]
        DRAFT_WITH_US["services"][1],
    ]
    return {"author": "jake", "services": services}


def test_draft_naming_an_unknown_service_group_is_rejected_at_draft_time(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/rulebook/drafts", json=_draft_with_service_groups(["MYSTERY"])
    )

    assert response.status_code == 422
    assert "MYSTERY" in response.text


def test_draft_may_name_a_service_group_from_the_catalogue(client: TestClient) -> None:
    response = client.post(
        "/api/rulebook/drafts", json=_draft_with_service_groups(["ECONOMY"])
    )

    assert response.status_code == 201


def test_dry_run_order_list_is_bounded_like_limit(client: TestClient) -> None:
    client.get("/api/rulebook/active")
    draft = client.post("/api/rulebook/drafts", json=DRAFT_WITH_US).json()["version"]

    response = client.post(
        f"/api/rulebook/versions/{draft}/dry-run",
        json={"order_numbers": [f"9{n:07d}" for n in range(501)]},
    )

    assert response.status_code == 422


def test_banded_costs_survive_publish_and_drive_live_allocation(
    client: TestClient,
) -> None:
    """Cost bands round-trip through the JSON rulebook column (no migration,
    ADR 0007 data-is-data) and the live allocation selects on the calculated
    Delivery Cost, excluding an uncostable service loudly."""
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "BANDED",
                "carrier": "dropout",
                "name": "Banded",
                "weight_min_kg": "0",
                "weight_max_kg": "30",
                "countries": ["GB"],
                "cost": "99.00",  # the flat fallback must NOT be used
                "cost_bands": [
                    {
                        "cost_type": "consignment_weight",
                        "min_weight_kg": "0",
                        "max_weight_kg": "30",
                        "charge": "3.00",
                    },
                    {"cost_type": "fuel_surcharge", "percentage": "10"},
                ],
                "tie_break_order": 1,
            },
            {
                "code": "FLAT",
                "carrier": "dropout",
                "name": "Flat",
                "weight_min_kg": "0",
                "weight_max_kg": "30",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 2,
            },
            {
                "code": "COSTLESS",
                "carrier": "dropout",
                "name": "Bands never match",
                "weight_min_kg": "0",
                "weight_max_kg": "30",
                "countries": ["GB"],
                "cost": "0.01",
                "cost_bands": [
                    {
                        "cost_type": "consignment_weight",
                        "min_weight_kg": "0",
                        "max_weight_kg": "1",
                        "charge": "1.00",
                    }
                ],
                "tie_break_order": 3,
            },
        ],
    }
    client.get("/api/rulebook/active")
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200

    response = client.post("/api/consignments", json=GB_CONSIGNMENT)

    assert response.status_code == 201
    body = response.json()
    assert body["service"] == "BANDED"
    allocation = body["allocation"]
    assert Decimal(str(allocation["selected_cost"])) == Decimal("3.30")
    costless = next(
        r for r in allocation["service_results"] if r["service_code"] == "COSTLESS"
    )
    assert costless["eligible"] is False
    no_cost = next(c for c in costless["checks"] if c["name"] == "no-cost-data")
    assert no_cost["ok"] is False
    assert no_cost["actual"] == "no cost data"


def test_dry_run_replays_area_facts_for_historical_orders(
    client: TestClient,
) -> None:
    """Replaying the LIVE version must reproduce the live outcome: an order
    rejected because its postcode resolved to a blocked area must stay
    rejected in the replay, which requires re-resolving areas from the
    stored postcode rather than evaluating optimistically."""
    area = client.post(
        "/api/shipping-areas",
        json={
            "code": "HIGHLANDS",
            "name": "Scottish Highlands",
            "country": "GB",
            "prefixes": ["IV"],
        },
    )
    assert area.status_code == 201
    draft = {
        "author": "jake",
        "services": [
            {
                **DRAFT_WITH_US["services"][0],  # type: ignore[dict-item]
                "areas_blocked": ["HIGHLANDS"],
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200
    blocked = client.post(
        "/api/consignments",
        json={**GB_CONSIGNMENT, "order_number": "95000254590", "postcode": "IV1 2AB"},
    )
    assert blocked.json()["status"] == "rejected"

    replay = client.post(
        f"/api/rulebook/versions/{version}/dry-run",
        json={"order_numbers": ["95000254590"]},
    ).json()

    assert replay["results"][0] == {
        "order_number": "95000254590",
        "current_service": None,
        "draft_service": None,
        "changed": False,
    }
