"""The carriers admin edge: the carrier list and per-carrier config read that the
config surface drives. Writes stay on the existing PUT/PATCH config routes."""

from fastapi.testclient import TestClient

DEFINITION = {
    "carrier": "acme",
    "name": "Acme",
    "auth": {"scheme": "query_key", "param": "key", "secret": "config.api_key"},
    "operations": {
        "book": {
            "steps": [
                {
                    "name": "book",
                    "transport": "http",
                    "request": {
                        "method": "POST",
                        "url": "config.base_url",
                        "content_type": "json",
                        "mapping": [
                            {"target": "order", "source": "shipment.order_number"}
                        ],
                    },
                }
            ]
        }
    },
}


def test_carriers_lists_the_union_of_definitions_and_configs(
    client: TestClient,
) -> None:
    # A carrier can exist as config only (credentials stored ahead of the
    # definition, the 5c onboarding order) or as definitions only.
    client.put("/api/carriers/configonly/config", json={"api_key": "K-1"})
    client.post(
        "/api/carriers/acme/definitions/drafts",
        json={"author": "jake", "definition": DEFINITION},
    )

    response = client.get("/api/carriers")
    assert response.status_code == 200
    by_code = {row["carrier"]: row for row in response.json()}

    assert by_code["configonly"]["active_version"] is None
    assert by_code["acme"]["active_version"] is None  # drafted, not yet published
    # The seeded carrier ships with an active definition.
    assert by_code["dropout"]["active_version"] is not None

    client.put(
        "/api/carriers/acme/config",
        json={"api_key": "K-1", "base_url": "https://api.acme.example"},
    )
    client.post("/api/carriers/acme/definitions/versions/1/publish")
    listed = {row["carrier"]: row for row in client.get("/api/carriers").json()}
    assert listed["acme"]["active_version"] == 1


def test_config_read_reports_stored_values_and_whats_missing(
    client: TestClient,
) -> None:
    client.put("/api/carriers/acme/config", json={"api_key": "K-1"})
    client.post(
        "/api/carriers/acme/definitions/drafts",
        json={"author": "jake", "definition": DEFINITION},
    )

    # No active definition yet: nothing can be missing.
    before = client.get("/api/carriers/acme/config").json()
    assert before == {"carrier": "acme", "config": {"api_key": "K-1"}, "missing": []}

    client.put(
        "/api/carriers/acme/config",
        json={"api_key": "K-1", "base_url": "https://api.acme.example"},
    )
    client.post("/api/carriers/acme/definitions/versions/1/publish")
    client.put("/api/carriers/acme/config", json={"api_key": "K-1"})

    after = client.get("/api/carriers/acme/config").json()
    assert after["config"] == {"api_key": "K-1"}
    assert after["missing"] == ["base_url"]


def test_config_read_is_empty_for_an_unknown_carrier(client: TestClient) -> None:
    # Config may precede everything else (the onboarding order), so an unknown
    # carrier reads as empty rather than 404.
    response = client.get("/api/carriers/nonesuch/config")
    assert response.status_code == 200
    assert response.json() == {"carrier": "nonesuch", "config": {}, "missing": []}
