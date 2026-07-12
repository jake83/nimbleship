from fastapi.testclient import TestClient

SATURDAY = {
    "code": "saturday",
    "name": "Saturday",
    "description": "Delivered on a Saturday.",
}


def test_listing_seeds_the_demo_catalogue_on_a_fresh_install(
    client: TestClient,
) -> None:
    response = client.get("/api/propositions")

    assert response.status_code == 200
    body = response.json()
    assert [p["code"] for p in body] == ["economy", "next-day"]
    for proposition in body:
        assert proposition["name"] != ""
        assert proposition["description"] != ""


def test_creating_a_proposition_adds_it_to_the_catalogue(client: TestClient) -> None:
    created = client.post("/api/propositions", json=SATURDAY)

    assert created.status_code == 201
    assert created.json() == SATURDAY
    codes = [p["code"] for p in client.get("/api/propositions").json()]
    assert codes == ["economy", "next-day", "saturday"]


def test_creating_a_duplicate_code_conflicts(client: TestClient) -> None:
    client.post("/api/propositions", json=SATURDAY)

    response = client.post("/api/propositions", json=SATURDAY)

    assert response.status_code == 409


def test_updating_a_proposition_changes_name_and_description(
    client: TestClient,
) -> None:
    client.post("/api/propositions", json=SATURDAY)

    response = client.put(
        "/api/propositions/saturday",
        json={"name": "Saturday Delivery", "description": "Weekend promise."},
    )

    assert response.status_code == 200
    assert response.json() == {
        "code": "saturday",
        "name": "Saturday Delivery",
        "description": "Weekend promise.",
    }
    listed = {p["code"]: p for p in client.get("/api/propositions").json()}
    assert listed["saturday"]["name"] == "Saturday Delivery"


def test_updating_an_unknown_proposition_is_not_found(client: TestClient) -> None:
    response = client.put(
        "/api/propositions/nope", json={"name": "Nope", "description": ""}
    )

    assert response.status_code == 404


def test_a_blank_code_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/propositions", json={"code": "", "name": "Blank", "description": ""}
    )

    assert response.status_code == 422


def test_a_code_with_spaces_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/propositions",
        json={"code": "next day", "name": "Next Day", "description": ""},
    )

    assert response.status_code == 422
