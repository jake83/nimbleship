from decimal import Decimal

import pytest
from pydantic import ValidationError

from nimbleship.domain.allocation import (
    Rulebook,
    ServiceDeclaration,
    Shipment,
    allocate,
)


def service(**overrides: object) -> ServiceDeclaration:
    defaults: dict[str, object] = {
        "code": "STD",
        "carrier": "dropout",
        "name": "Standard",
        "weight_min_kg": Decimal("0"),
        "weight_max_kg": Decimal("30"),
        "countries": ["GB"],
        "cost": Decimal("4.50"),
        "tie_break_order": 1,
    }
    defaults.update(overrides)
    return ServiceDeclaration(**defaults)  # type: ignore[arg-type]


def shipment(**overrides: object) -> Shipment:
    defaults: dict[str, object] = {
        "order_number": "95000254580",
        "destination_country": "GB",
        "total_weight_kg": Decimal("10"),
        "parcel_count": 1,
    }
    defaults.update(overrides)
    return Shipment(**defaults)  # type: ignore[arg-type]


def test_service_matching_all_declarations_is_eligible_and_selected() -> None:
    rulebook = Rulebook(version=1, services=[service()])

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    assert result.selected.code == "STD"
    assert result.service_results[0].eligible is True


def test_cheapest_eligible_service_wins() -> None:
    rulebook = Rulebook(
        version=1,
        services=[
            service(code="PRICY", cost=Decimal("12.00"), tie_break_order=1),
            service(code="CHEAP", cost=Decimal("4.50"), tie_break_order=2),
        ],
    )

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    assert result.selected.code == "CHEAP"


def test_equal_costs_fall_back_to_tie_break_order() -> None:
    rulebook = Rulebook(
        version=1,
        services=[
            service(code="SECOND", cost=Decimal("5.00"), tie_break_order=2),
            service(code="FIRST", cost=Decimal("5.00"), tie_break_order=1),
        ],
    )

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    assert result.selected.code == "FIRST"


def test_country_outside_declaration_excludes_service() -> None:
    rulebook = Rulebook(version=1, services=[service(countries=["GB"])])

    result = allocate(rulebook, shipment(destination_country="FR"))

    assert result.selected is None
    [service_result] = result.service_results
    assert service_result.eligible is False
    failed = [c for c in service_result.checks if not c.ok]
    assert [c.name for c in failed] == ["country"]


def test_weight_above_declaration_excludes_service() -> None:
    rulebook = Rulebook(version=1, services=[service(weight_max_kg=Decimal("30"))])

    result = allocate(rulebook, shipment(total_weight_kg=Decimal("45")))

    assert result.selected is None
    failed = [c for c in result.service_results[0].checks if not c.ok]
    assert [c.name for c in failed] == ["weight"]


def test_trace_records_every_check_for_every_service() -> None:
    rulebook = Rulebook(
        version=1,
        services=[
            service(code="A", tie_break_order=1),
            service(code="B", countries=["FR"], tie_break_order=2),
        ],
    )

    result = allocate(rulebook, shipment())

    assert {r.service_code for r in result.service_results} == {"A", "B"}
    for service_result in result.service_results:
        assert {c.name for c in service_result.checks} == {
            "country",
            "weight",
            "dimension",
        }
        for check in service_result.checks:
            assert check.actual != ""
            assert check.expected != ""


def test_no_eligible_services_reports_reason() -> None:
    rulebook = Rulebook(version=1, services=[service(countries=["FR"])])

    result = allocate(rulebook, shipment(destination_country="GB"))

    assert result.selected is None
    assert result.reason == "no eligible services"


def test_selection_reason_names_cost_policy() -> None:
    rulebook = Rulebook(version=1, services=[service()])

    result = allocate(rulebook, shipment())

    assert result.reason == "cheapest eligible service"


def test_selected_carries_the_full_service_declaration() -> None:
    rulebook = Rulebook(version=1, services=[service()])

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    assert result.selected.carrier == "dropout"
    assert result.selected.cost == Decimal("4.50")


def test_duplicate_service_codes_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate service code"):
        Rulebook(
            version=1,
            services=[service(code="SAME"), service(code="SAME", tie_break_order=2)],
        )


def test_duplicate_tie_break_orders_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate tie-break order"):
        Rulebook(
            version=1,
            services=[
                service(code="A", tie_break_order=1),
                service(code="B", tie_break_order=1),
            ],
        )


def test_a_rulebook_must_declare_at_least_one_service() -> None:
    with pytest.raises(ValidationError, match="at least 1 item"):
        Rulebook(version=1, services=[])


def test_old_stored_rulebooks_still_validate() -> None:
    legacy_shape = {
        "code": "OLD",
        "carrier": "dropout",
        "name": "Stored before Phase 2 fields existed",
        "weight_min_kg": "0",
        "weight_max_kg": "30",
        "countries": ["GB"],
        "cost": "4.50",
        "tie_break_order": 1,
    }

    declaration = ServiceDeclaration.model_validate(legacy_shape)

    assert declaration.max_dimension_cm is None
    assert declaration.areas_blocked == []
    assert declaration.propositions == []


def test_dimension_over_service_limit_excludes_service() -> None:
    rulebook = Rulebook(version=1, services=[service(max_dimension_cm=Decimal("120"))])

    result = allocate(rulebook, shipment(max_dimension_cm=Decimal("150")))

    assert result.selected is None
    failed = [c for c in result.service_results[0].checks if not c.ok]
    assert [c.name for c in failed] == ["dimension"]


def test_unknown_dimension_is_optimistically_eligible() -> None:
    rulebook = Rulebook(version=1, services=[service(max_dimension_cm=Decimal("120"))])

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    dimension = next(
        c for c in result.service_results[0].checks if c.name == "dimension"
    )
    assert dimension.ok is True
    assert "unknown" in dimension.actual


def test_service_without_dimension_limit_accepts_anything() -> None:
    rulebook = Rulebook(version=1, services=[service()])

    result = allocate(rulebook, shipment(max_dimension_cm=Decimal("400")))

    assert result.selected is not None
