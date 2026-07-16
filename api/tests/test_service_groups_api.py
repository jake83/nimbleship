import pytest
from fastapi.testclient import TestClient

AFTERSALE = {
    "code": "AFTERSALE",
    "name": "Aftersale",
    "description": "Priority services to reach the customer quicker.",
}


def test_listing_seeds_the_demo_catalogue_on_a_fresh_install(
    client: TestClient,
) -> None:
    response = client.get("/api/service-groups")

    assert response.status_code == 200
    body = response.json()
    assert [g["code"] for g in body] == ["ECONOMY", "NEXTDAY"]
    for group in body:
        assert group["name"] != ""


def test_creating_a_service_group_adds_it_to_the_catalogue(client: TestClient) -> None:
    created = client.post("/api/service-groups", json=AFTERSALE)

    assert created.status_code == 201
    assert created.json() == AFTERSALE
    codes = [g["code"] for g in client.get("/api/service-groups").json()]
    assert codes == ["AFTERSALE", "ECONOMY", "NEXTDAY"]


def test_creating_a_duplicate_code_conflicts(client: TestClient) -> None:
    client.post("/api/service-groups", json=AFTERSALE)

    response = client.post("/api/service-groups", json=AFTERSALE)

    assert response.status_code == 409


def test_updating_a_service_group_changes_name_and_description(
    client: TestClient,
) -> None:
    client.post("/api/service-groups", json=AFTERSALE)

    response = client.put(
        "/api/service-groups/AFTERSALE",
        json={"name": "After Sale", "description": "Expedited replacements."},
    )

    assert response.status_code == 200
    listed = {g["code"]: g for g in client.get("/api/service-groups").json()}
    assert listed["AFTERSALE"]["name"] == "After Sale"


def test_updating_an_unknown_service_group_is_not_found(client: TestClient) -> None:
    response = client.put(
        "/api/service-groups/nope", json={"name": "Nope", "description": ""}
    )

    assert response.status_code == 404


def test_losing_a_duplicate_create_race_still_returns_409(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import nimbleship.domain.service_groups as service_groups_module

    client.post("/api/service-groups", json=AFTERSALE)
    # Simulate the race: the pre-check misses the row another request already
    # committed, so the primary key is the last line of defence (PR #6 pattern).
    monkeypatch.setattr(
        service_groups_module, "_code_taken", lambda session, code: False
    )

    response = client.post("/api/service-groups", json=AFTERSALE)

    assert response.status_code == 409


def test_a_code_with_spaces_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/service-groups",
        json={"code": "next day", "name": "Next Day", "description": ""},
    )

    assert response.status_code == 422
