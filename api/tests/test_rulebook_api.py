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
    assert retry.json()["status"] == "allocated"
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
