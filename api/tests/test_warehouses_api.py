from fastapi.testclient import TestClient

WAREHOUSE = {
    "code": "MAIN",
    "name": "Main Warehouse",
    "company_name": "Acme Fulfilment Ltd",
    "phone": "01onetwothree",
    "email": "dispatch@example.com",
    "address_lines": ["Unit 5, Trading Estate", "Industry Way"],
    "postcode": "LE1 1AA",
    "country": "GB",
}


def test_creating_a_warehouse_defaults_collection_days_to_weekdays(
    client: TestClient,
) -> None:
    response = client.post("/api/warehouses", json=WAREHOUSE)

    assert response.status_code == 201
    body = response.json()
    assert body["code"] == "MAIN"
    assert body["name"] == "Main Warehouse"
    assert body["address_lines"] == ["Unit 5, Trading Estate", "Industry Way"]
    assert body["collection_days"] == {
        "monday": True,
        "tuesday": True,
        "wednesday": True,
        "thursday": True,
        "friday": True,
        "saturday": False,
        "sunday": False,
    }
    assert body["holidays"] == []


def test_creating_a_warehouse_with_calendar_round_trips_it(
    client: TestClient,
) -> None:
    payload = {
        **WAREHOUSE,
        "collection_days": {"saturday": True, "friday": False},
        "holidays": [
            {"date": "2026-12-28", "description": "Boxing Day (substitute)"},
            {"date": "2026-12-25", "description": "Christmas Day"},
        ],
    }

    response = client.post("/api/warehouses", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["collection_days"]["saturday"] is True
    assert body["collection_days"]["friday"] is False
    # Holidays come back sorted by date regardless of payload order.
    assert body["holidays"] == [
        {"date": "2026-12-25", "description": "Christmas Day"},
        {"date": "2026-12-28", "description": "Boxing Day (substitute)"},
    ]


def test_duplicate_warehouse_code_conflicts(client: TestClient) -> None:
    client.post("/api/warehouses", json=WAREHOUSE)

    response = client.post("/api/warehouses", json=WAREHOUSE)

    assert response.status_code == 409


def test_duplicate_holiday_dates_are_rejected(client: TestClient) -> None:
    payload = {
        **WAREHOUSE,
        "holidays": [
            {"date": "2026-12-25", "description": "Christmas Day"},
            {"date": "2026-12-25", "description": "Christmas Day again"},
        ],
    }

    response = client.post("/api/warehouses", json=payload)

    assert response.status_code == 422


def test_listing_warehouses_orders_by_code(client: TestClient) -> None:
    client.post("/api/warehouses", json={**WAREHOUSE, "code": "SOUTH"})
    client.post("/api/warehouses", json={**WAREHOUSE, "code": "NORTH"})

    response = client.get("/api/warehouses")

    assert response.status_code == 200
    assert [w["code"] for w in response.json()] == ["NORTH", "SOUTH"]


def test_fetching_a_warehouse_by_code(client: TestClient) -> None:
    client.post("/api/warehouses", json=WAREHOUSE)

    response = client.get("/api/warehouses/MAIN")

    assert response.status_code == 200
    assert response.json()["name"] == "Main Warehouse"


def test_unknown_warehouse_is_not_found(client: TestClient) -> None:
    assert client.get("/api/warehouses/NOPE").status_code == 404
    assert client.put("/api/warehouses/NOPE", json=WAREHOUSE).status_code == 404
    assert client.delete("/api/warehouses/NOPE").status_code == 404


def test_updating_a_warehouse_replaces_details_and_calendar(
    client: TestClient,
) -> None:
    client.post(
        "/api/warehouses",
        json={**WAREHOUSE, "holidays": [{"date": "2026-12-25"}]},
    )

    response = client.put(
        "/api/warehouses/MAIN",
        json={
            **WAREHOUSE,
            "name": "Main Warehouse (renamed)",
            "collection_days": {"monday": False},
            "holidays": [{"date": "2027-01-01", "description": "New Year"}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Main Warehouse (renamed)"
    assert body["collection_days"]["monday"] is False
    assert body["holidays"] == [{"date": "2027-01-01", "description": "New Year"}]

    fetched = client.get("/api/warehouses/MAIN").json()
    assert fetched == body


def test_renaming_a_warehouse_code_to_an_existing_code_conflicts(
    client: TestClient,
) -> None:
    client.post("/api/warehouses", json={**WAREHOUSE, "code": "NORTH"})
    client.post("/api/warehouses", json={**WAREHOUSE, "code": "SOUTH"})

    response = client.put("/api/warehouses/SOUTH", json={**WAREHOUSE, "code": "NORTH"})

    assert response.status_code == 409


def test_deleting_a_warehouse_removes_it_and_its_calendar(
    client: TestClient,
) -> None:
    client.post(
        "/api/warehouses",
        json={**WAREHOUSE, "holidays": [{"date": "2026-12-25"}]},
    )

    response = client.delete("/api/warehouses/MAIN")

    assert response.status_code == 204
    assert client.get("/api/warehouses/MAIN").status_code == 404
    # The code is free for reuse: the calendar rows went with the warehouse.
    assert client.post("/api/warehouses", json=WAREHOUSE).status_code == 201


def test_warehouse_without_address_lines_is_rejected(client: TestClient) -> None:
    response = client.post("/api/warehouses", json={**WAREHOUSE, "address_lines": []})

    assert response.status_code == 422
