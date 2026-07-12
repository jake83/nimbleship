"""CRUD for Shipping Areas: named geographies defined by postcode prefixes
(CONTEXT.md). The area is the named thing; prefixes are its definition."""

from fastapi.testclient import TestClient

HIGHLANDS = {
    "code": "HIGHLANDS",
    "name": "Scottish Highlands",
    "country": "GB",
    "prefixes": ["IV", "KW", "PH19"],
}


def test_creating_a_shipping_area_returns_it(client: TestClient) -> None:
    response = client.post("/api/shipping-areas", json=HIGHLANDS)

    assert response.status_code == 201
    assert response.json() == {
        "code": "HIGHLANDS",
        "name": "Scottish Highlands",
        "country": "GB",
        "prefixes": ["IV", "KW", "PH19"],
    }


def test_listing_returns_created_areas(client: TestClient) -> None:
    client.post("/api/shipping-areas", json=HIGHLANDS)
    client.post(
        "/api/shipping-areas",
        json={
            "code": "NORTHERN-IRELAND",
            "name": "Northern Ireland",
            "country": "GB",
            "prefixes": ["BT"],
        },
    )

    response = client.get("/api/shipping-areas")

    assert response.status_code == 200
    assert [area["code"] for area in response.json()] == [
        "HIGHLANDS",
        "NORTHERN-IRELAND",
    ]


def test_duplicate_area_code_conflicts(client: TestClient) -> None:
    client.post("/api/shipping-areas", json=HIGHLANDS)

    response = client.post("/api/shipping-areas", json=HIGHLANDS)

    assert response.status_code == 409


def test_updating_replaces_name_country_and_prefixes(client: TestClient) -> None:
    client.post("/api/shipping-areas", json=HIGHLANDS)

    response = client.put(
        "/api/shipping-areas/HIGHLANDS",
        json={
            "name": "Highlands and Islands",
            "country": "GB",
            "prefixes": ["IV", "HS"],
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "code": "HIGHLANDS",
        "name": "Highlands and Islands",
        "country": "GB",
        "prefixes": ["HS", "IV"],
    }
    listed = client.get("/api/shipping-areas").json()
    assert listed[0]["prefixes"] == ["HS", "IV"]


def test_updating_an_unknown_area_is_404(client: TestClient) -> None:
    response = client.put(
        "/api/shipping-areas/NOWHERE",
        json={"name": "Nowhere", "country": "GB", "prefixes": ["ZZ"]},
    )

    assert response.status_code == 404


def test_prefixes_are_normalised_and_deduplicated(client: TestClient) -> None:
    response = client.post(
        "/api/shipping-areas",
        json={
            "code": "HIGHLANDS",
            "name": "Scottish Highlands",
            "country": "gb",
            "prefixes": [" iv ", "IV", "kw"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["country"] == "GB"
    assert body["prefixes"] == ["IV", "KW"]


def test_an_area_requires_at_least_one_prefix(client: TestClient) -> None:
    response = client.post(
        "/api/shipping-areas",
        json={
            "code": "HIGHLANDS",
            "name": "Scottish Highlands",
            "country": "GB",
            "prefixes": [],
        },
    )

    assert response.status_code == 422


def test_a_blank_prefix_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/shipping-areas",
        json={
            "code": "HIGHLANDS",
            "name": "Scottish Highlands",
            "country": "GB",
            "prefixes": ["   "],
        },
    )

    assert response.status_code == 422
