"""Carrier Definition storage rails: per-carrier versioned documents on the
draft/test/publish pattern (ADR 0003 via ADR 0009), with Golden Replay as
the test step - draft renders diffed against the active definition's."""

from fastapi.testclient import TestClient

TEST_CARRIER_DEFINITION = {
    "carrier": "testcarrier",
    "name": "Test Carrier",
    "auth": {"scheme": "query_key", "param": "key", "secret": "config.api_key"},
    "operations": {
        "book": {
            "steps": [
                {
                    "name": "save",
                    "transport": "http",
                    "request": {
                        "method": "POST",
                        "url": "config.base_url",
                        "content_type": "json",
                        "mapping": [
                            {
                                "target": "order",
                                "source": "shipment.order_number",
                            },
                            {"target": "channel", "const": "nimbleship"},
                        ],
                    },
                }
            ],
        }
    },
}

CONSIGNMENT = {
    "order_number": "95000254580",
    "recipient_name": "John Doe",
    "address_lines": ["10 Downing Street", "London"],
    "postcode": "SW1A 2AA",
    "destination_country": "GB",
    "parcels": [{"weight_kg": "4.2"}],
}


def _publish_v1_with_config(client: TestClient) -> None:
    client.put(
        "/api/carriers/testcarrier/config",
        json={"api_key": "K-1", "base_url": "https://api.test.example"},
    )
    client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    )
    client.post("/api/carriers/testcarrier/definitions/versions/1/publish")


def test_dropout_definition_seeds_and_is_active(client: TestClient) -> None:
    response = client.get("/api/carriers/dropout/definitions/active")

    assert response.status_code == 200
    body = response.json()
    assert body["version"] == 1
    assert body["definition"]["carrier"] == "dropout"
    assert body["definition"]["operations"]["book"]["label"]["source"] == (
        "local_render"
    )


def test_draft_publish_lifecycle_per_carrier(client: TestClient) -> None:
    created = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    )
    assert created.status_code == 201
    assert created.json() == {
        "carrier": "testcarrier",
        "version": 1,
        "status": "draft",
        "author": "jake",
    }

    published = client.post("/api/carriers/testcarrier/definitions/versions/1/publish")
    assert published.status_code == 200

    active = client.get("/api/carriers/testcarrier/definitions/active").json()
    assert active["version"] == 1

    versions = client.get("/api/carriers/testcarrier/definitions/versions").json()
    assert [(v["version"], v["status"]) for v in versions] == [(1, "published")]


def test_draft_carrier_must_match_the_url(client: TestClient) -> None:
    response = client.post(
        "/api/carriers/other/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    )

    assert response.status_code == 422


def test_invalid_definition_is_rejected_at_draft(client: TestClient) -> None:
    bad = {
        **TEST_CARRIER_DEFINITION,
        "auth": {"scheme": "query_key", "param": "key", "secret": "typo.api_key"},
    }

    response = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": bad},
    )

    assert response.status_code == 422
    assert "unknown source root" in response.text


def test_publishing_a_stale_draft_conflicts(client: TestClient) -> None:
    for _ in range(2):
        client.post(
            "/api/carriers/testcarrier/definitions/drafts",
            json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
        )
    client.post("/api/carriers/testcarrier/definitions/versions/2/publish")

    response = client.post("/api/carriers/testcarrier/definitions/versions/1/publish")

    assert response.status_code == 409


def test_golden_replay_diffs_draft_renders_against_active(
    client: TestClient,
) -> None:
    _publish_v1_with_config(client)
    client.post("/api/consignments", json=CONSIGNMENT)

    changed = {
        **TEST_CARRIER_DEFINITION,
        "operations": {
            "book": {
                "steps": [
                    {
                        "name": "save",
                        "transport": "http",
                        "request": {
                            "method": "POST",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "order",
                                    "source": "shipment.order_number",
                                },
                                {"target": "channel", "const": "CHANGED"},
                            ],
                        },
                    }
                ],
            }
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": changed},
    ).json()

    replay = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/replay",
        json={},
    )

    assert replay.status_code == 200
    body = replay.json()
    assert body["total"] == 1
    assert body["changed"] == 1
    [result] = body["results"]
    assert result["order_number"] == "95000254580"
    assert result["changed"] is True
    assert result["differences"] == [
        {
            "step": "save",
            "field": "body.channel",
            "active": "nimbleship",
            "draft": "CHANGED",
        }
    ]


def test_golden_replay_of_an_identical_draft_reports_no_changes(
    client: TestClient,
) -> None:
    _publish_v1_with_config(client)
    client.post("/api/consignments", json=CONSIGNMENT)
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": TEST_CARRIER_DEFINITION},
    ).json()

    replay = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/replay",
        json={},
    ).json()

    assert replay["changed"] == 0


def test_a_definition_without_a_book_operation_fails_loudly_at_dispatch(
    client: TestClient,
) -> None:
    trackonly = {
        "carrier": "trackonly",
        "name": "Track Only",
        "auth": {"scheme": "none"},
        "operations": {
            "track": {
                "steps": [
                    {
                        "name": "status",
                        "transport": "http",
                        "request": {
                            "method": "GET",
                            "url": "config.base_url",
                            "content_type": "json",
                            "mapping": [
                                {
                                    "target": "order",
                                    "source": "shipment.order_number",
                                }
                            ],
                        },
                    }
                ],
            }
        },
    }
    client.put("/api/carriers/trackonly/config", json={"base_url": "https://x"})
    client.post(
        "/api/carriers/trackonly/definitions/drafts",
        json={"author": "jake", "definition": trackonly},
    )
    client.post("/api/carriers/trackonly/definitions/versions/1/publish")
    draft = {
        "author": "jake",
        "services": [
            {
                "code": "TRACKONLY-STD",
                "carrier": "trackonly",
                "name": "Bookless",
                "weight_min_kg": "0",
                "weight_max_kg": "999",
                "countries": ["GB"],
                "cost": "4.50",
                "tie_break_order": 1,
            }
        ],
    }
    version = client.post("/api/rulebook/drafts", json=draft).json()["version"]
    client.post(f"/api/rulebook/versions/{version}/publish")

    response = client.post("/api/consignments", json=CONSIGNMENT)

    assert response.status_code == 500
    assert "book" in response.text
    assert "trackonly" in response.text


def test_publish_refuses_a_draft_whose_renders_error(client: TestClient) -> None:
    """ADR 0009: a green replay (renders succeed - diffs are fine, errors
    are not) is required to publish. The gate runs inline at publish time
    against recent consignments."""
    _publish_v1_with_config(client)
    client.post("/api/consignments", json=CONSIGNMENT)

    broken = {
        **TEST_CARRIER_DEFINITION,
        "auth": {
            "scheme": "query_key",
            "param": "key",
            "secret": "config.missing_key",
        },
    }
    draft = client.post(
        "/api/carriers/testcarrier/definitions/drafts",
        json={"author": "jake", "definition": broken},
    ).json()

    response = client.post(
        f"/api/carriers/testcarrier/definitions/versions/{draft['version']}/publish"
    )

    assert response.status_code == 409
    assert "render" in response.text.lower()
    assert "missing_key" in response.text
