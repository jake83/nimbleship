"""The /api/quotes projection (chunk D): eligible services with their
Delivery Charges for a shipment-shaped payload - the seed of the checkout
endpoint (ADR 0007's checkout moment)."""

from fastapi.testclient import TestClient

GB_CHARGE_BANDS = [
    {
        "scope_type": "area",
        "scope_code": "NI",
        "min_weight_kg": "0",
        "max_weight_kg": "30",
        "charge": "14.99",
    },
    {
        "scope_type": "country",
        "scope_code": "GB",
        "min_weight_kg": "0",
        "max_weight_kg": "10",
        "charge": "5.99",
    },
    {
        "scope_type": "country",
        "scope_code": "GB",
        "min_weight_kg": "10",
        "max_weight_kg": "30",
        "charge": "8.99",
        "additional_charge": "0.50",
    },
    {
        "scope_type": "all",
        "scope_code": None,
        "min_weight_kg": "0",
        "max_weight_kg": "999",
        "charge": "19.99",
    },
]

DRAFT_WITH_CHARGES = {
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
            "charge_bands": GB_CHARGE_BANDS,
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
        },
    ],
}

GB_QUOTE_REQUEST = {
    "order_number": "BASKET-1001",
    "destination_country": "GB",
    "total_weight_kg": "4.2",
    "parcel_count": 1,
}


def publish_charged_rulebook(client: TestClient) -> None:
    version = client.post("/api/rulebook/drafts", json=DRAFT_WITH_CHARGES).json()[
        "version"
    ]
    assert client.post(f"/api/rulebook/versions/{version}/publish").status_code == 200


def test_quote_returns_eligible_services_with_charges(client: TestClient) -> None:
    publish_charged_rulebook(client)

    response = client.post("/api/quotes", json=GB_QUOTE_REQUEST)

    assert response.status_code == 200
    body = response.json()
    assert body["rulebook_version"] == 2
    assert body["services"] == [
        {
            "code": "DROPOUT-STD",
            "carrier": "dropout",
            "name": "Drop Out Standard",
            "charge": "5.99",
        },
        {
            "code": "DROPOUT-XL",
            "carrier": "dropout",
            "name": "Drop Out Extra Large",
            "charge": None,
        },
    ]


def test_quote_excludes_ineligible_services(client: TestClient) -> None:
    publish_charged_rulebook(client)

    response = client.post(
        "/api/quotes",
        json={**GB_QUOTE_REQUEST, "total_weight_kg": "45"},
    )

    # 45kg is over DROPOUT-STD's 30kg ceiling; only the XL service remains.
    assert [s["code"] for s in response.json()["services"]] == ["DROPOUT-XL"]


def test_quote_prices_by_shipping_area_before_country(client: TestClient) -> None:
    publish_charged_rulebook(client)

    response = client.post(
        "/api/quotes",
        json={**GB_QUOTE_REQUEST, "shipping_areas": ["NI"]},
    )

    charges = {s["code"]: s["charge"] for s in response.json()["services"]}
    assert charges["DROPOUT-STD"] == "14.99"


def test_quote_charges_the_weight_band_with_additional_per_kg(
    client: TestClient,
) -> None:
    publish_charged_rulebook(client)

    response = client.post(
        "/api/quotes",
        json={**GB_QUOTE_REQUEST, "total_weight_kg": "12.5"},
    )

    charges = {s["code"]: s["charge"] for s in response.json()["services"]}
    assert charges["DROPOUT-STD"] == "10.49"  # 8.99 + ceil(2.5) * 0.50


def test_quote_with_no_eligible_services_returns_an_empty_list(
    client: TestClient,
) -> None:
    publish_charged_rulebook(client)

    response = client.post(
        "/api/quotes",
        json={**GB_QUOTE_REQUEST, "destination_country": "US"},
    )

    assert response.status_code == 200
    assert response.json()["services"] == []


def test_quote_rejects_a_malformed_payload(client: TestClient) -> None:
    response = client.post("/api/quotes", json={"destination_country": "GB"})

    assert response.status_code == 422


def test_draft_with_invalid_charge_band_is_rejected_at_authoring(
    client: TestClient,
) -> None:
    bad_band = {
        "scope_type": "area",  # area scope without a scope_code
        "min_weight_kg": "0",
        "max_weight_kg": "30",
        "charge": "5.99",
    }
    services = [
        {
            **DRAFT_WITH_CHARGES["services"][0],  # type: ignore[dict-item]
            "charge_bands": [bad_band],
        }
    ]

    response = client.post(
        "/api/rulebook/drafts", json={"author": "jake", "services": services}
    )

    assert response.status_code == 422
    assert "scope_code" in response.text
