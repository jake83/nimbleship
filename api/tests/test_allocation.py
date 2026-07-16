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
            "proposition",
            "service_group",
            "girth",
            "area_blocked",
            "area_served",
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
    assert declaration.max_girth_cm is None
    assert declaration.areas_served is None
    assert declaration.areas_blocked == []
    assert declaration.propositions == []
    assert declaration.cost_bands is None
    assert declaration.charge_bands is None


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


def test_service_not_fulfilling_the_bought_proposition_is_excluded() -> None:
    rulebook = Rulebook(version=1, services=[service(propositions=["economy"])])

    result = allocate(rulebook, shipment(proposition="next-day"))

    assert result.selected is None
    failed = [c for c in result.service_results[0].checks if not c.ok]
    assert [c.name for c in failed] == ["proposition"]


def test_service_fulfilling_the_bought_proposition_is_eligible() -> None:
    rulebook = Rulebook(
        version=1, services=[service(propositions=["next-day", "economy"])]
    )

    result = allocate(rulebook, shipment(proposition="next-day"))

    assert result.selected is not None


def test_girth_over_service_limit_excludes_service() -> None:
    rulebook = Rulebook(version=1, services=[service(max_girth_cm=Decimal("300"))])

    result = allocate(rulebook, shipment(max_girth_cm=Decimal("350")))

    assert result.selected is None
    failed = [c for c in result.service_results[0].checks if not c.ok]
    assert [c.name for c in failed] == ["girth"]


def test_unknown_girth_is_optimistically_eligible() -> None:
    rulebook = Rulebook(version=1, services=[service(max_girth_cm=Decimal("300"))])

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    girth = next(c for c in result.service_results[0].checks if c.name == "girth")
    assert girth.ok is True
    assert "unknown" in girth.actual


def test_service_without_girth_limit_accepts_anything() -> None:
    rulebook = Rulebook(version=1, services=[service()])

    result = allocate(rulebook, shipment(max_girth_cm=Decimal("900")))

    assert result.selected is not None


def test_shipment_in_a_blocked_area_excludes_service() -> None:
    rulebook = Rulebook(version=1, services=[service(areas_blocked=["HIGHLANDS"])])

    result = allocate(rulebook, shipment(shipping_areas=["HIGHLANDS"]))

    assert result.selected is None
    failed = [c for c in result.service_results[0].checks if not c.ok]
    assert [c.name for c in failed] == ["area_blocked"]


def test_shipment_outside_blocked_areas_stays_eligible() -> None:
    rulebook = Rulebook(version=1, services=[service(areas_blocked=["HIGHLANDS"])])

    result = allocate(rulebook, shipment(shipping_areas=["NORTHERN-IRELAND"]))

    assert result.selected is not None


def test_unknown_proposition_is_optimistically_eligible() -> None:
    rulebook = Rulebook(version=1, services=[service(propositions=["next-day"])])

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    proposition = next(
        c for c in result.service_results[0].checks if c.name == "proposition"
    )
    assert proposition.ok is True
    assert "unknown" in proposition.actual


def test_service_declaring_no_propositions_is_unrestricted() -> None:
    rulebook = Rulebook(version=1, services=[service()])

    result = allocate(rulebook, shipment(proposition="next-day"))

    assert result.selected is not None


def test_no_matched_areas_is_optimistic_for_blocked_areas() -> None:
    rulebook = Rulebook(version=1, services=[service(areas_blocked=["HIGHLANDS"])])

    result = allocate(rulebook, shipment(shipping_areas=[]))

    assert result.selected is not None
    check = next(
        c for c in result.service_results[0].checks if c.name == "area_blocked"
    )
    assert check.ok is True
    assert "optimistic" in check.actual


def test_service_without_blocked_areas_accepts_any_area() -> None:
    rulebook = Rulebook(version=1, services=[service()])

    result = allocate(rulebook, shipment(shipping_areas=["HIGHLANDS"]))

    assert result.selected is not None


def weight_bands(charge: str, additional: str | None = None) -> list[dict[str, str]]:
    band = {
        "cost_type": "consignment_weight",
        "min_weight_kg": "0",
        "max_weight_kg": "30",
        "charge": charge,
    }
    if additional is not None:
        band["additional_charge"] = additional
    return [band]


def test_selection_uses_calculated_cost_when_cost_bands_are_present() -> None:
    """The flat cost is only the fallback: a banded service whose calculated
    cost undercuts a flat-cost rival wins even if its flat number is dearer."""
    rulebook = Rulebook(
        version=1,
        services=[
            service(code="FLAT", cost=Decimal("4.50"), tie_break_order=1),
            service(
                code="BANDED",
                cost=Decimal("99.00"),
                cost_bands=weight_bands("3.00"),
                tie_break_order=2,
            ),
        ],
    )

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    assert result.selected.code == "BANDED"
    assert result.selected_cost == Decimal("3.00")


def test_flat_cost_service_can_beat_a_banded_one() -> None:
    rulebook = Rulebook(
        version=1,
        services=[
            service(code="FLAT", cost=Decimal("4.50"), tie_break_order=1),
            service(
                code="BANDED",
                cost=Decimal("1.00"),
                cost_bands=weight_bands("2.00", additional="1.00"),
                tie_break_order=2,
            ),
        ],
    )

    result = allocate(rulebook, shipment(total_weight_kg=Decimal("10")))

    assert result.selected is not None
    assert result.selected.code == "FLAT"  # banded costs 2 + 10kg * 1 = 12
    assert result.selected_cost == Decimal("4.50")


def test_equal_calculated_costs_fall_back_to_tie_break_order() -> None:
    rulebook = Rulebook(
        version=1,
        services=[
            service(
                code="SECOND",
                cost=Decimal("99.00"),
                cost_bands=weight_bands("5.00"),
                tie_break_order=2,
            ),
            service(code="FIRST", cost=Decimal("5.00"), tie_break_order=1),
        ],
    )

    result = allocate(rulebook, shipment())

    assert result.selected is not None
    assert result.selected.code == "FIRST"


def test_service_with_no_applicable_cost_band_is_excluded_loudly() -> None:
    """ADR 0007: missing cost data is flagged in the trace, never silently
    skipped - the exclusion must be readable as no-cost-data."""
    rulebook = Rulebook(
        version=1,
        services=[
            service(
                code="COSTLESS",
                cost_bands=[
                    {
                        "cost_type": "consignment_weight",
                        "min_weight_kg": "0",
                        "max_weight_kg": "5",
                        "charge": "3.00",
                    }
                ],
                tie_break_order=1,
            ),
            service(code="FLAT", cost=Decimal("8.00"), tie_break_order=2),
        ],
    )

    result = allocate(rulebook, shipment(total_weight_kg=Decimal("10")))

    assert result.selected is not None
    assert result.selected.code == "FLAT"
    costless = next(r for r in result.service_results if r.service_code == "COSTLESS")
    assert costless.eligible is False
    no_cost = next(c for c in costless.checks if c.name == "no-cost-data")
    assert no_cost.ok is False
    assert no_cost.actual == "no cost data"


def test_all_eligible_services_lacking_cost_data_is_a_loud_rejection() -> None:
    rulebook = Rulebook(
        version=1,
        services=[service(code="COSTLESS", cost_bands=[], tie_break_order=1)],
    )

    result = allocate(rulebook, shipment())

    assert result.selected is None
    assert result.reason == "no cost data for any eligible service"
    assert result.service_results[0].eligible is False


def test_flat_cost_selection_reports_the_selected_cost() -> None:
    rulebook = Rulebook(version=1, services=[service(cost=Decimal("4.50"))])

    result = allocate(rulebook, shipment())

    assert result.selected_cost == Decimal("4.50")


def test_rejection_reports_no_selected_cost() -> None:
    rulebook = Rulebook(version=1, services=[service(countries=["FR"])])

    result = allocate(rulebook, shipment())

    assert result.selected_cost is None


def test_shipment_outside_served_areas_excludes_service() -> None:
    rulebook = Rulebook(version=1, services=[service(areas_served=["LONDON"])])

    result = allocate(rulebook, shipment(shipping_areas=["HIGHLANDS"]))

    assert result.selected is None
    failed = [c for c in result.service_results[0].checks if not c.ok]
    assert [c.name for c in failed] == ["area_served"]


def test_shipment_overlapping_served_areas_stays_eligible() -> None:
    rulebook = Rulebook(version=1, services=[service(areas_served=["LONDON"])])

    result = allocate(rulebook, shipment(shipping_areas=["LONDON", "ZONE-1"]))

    assert result.selected is not None


def test_no_matched_areas_is_optimistic_for_served_areas() -> None:
    rulebook = Rulebook(version=1, services=[service(areas_served=["LONDON"])])

    result = allocate(rulebook, shipment(shipping_areas=[]))

    assert result.selected is not None
    check = next(c for c in result.service_results[0].checks if c.name == "area_served")
    assert check.ok is True
    assert "optimistic" in check.actual


def test_service_serving_anywhere_accepts_any_area() -> None:
    rulebook = Rulebook(version=1, services=[service(areas_served=None)])

    result = allocate(rulebook, shipment(shipping_areas=["HIGHLANDS"]))

    assert result.selected is not None


def test_service_in_an_accepted_group_is_eligible() -> None:
    rulebook = Rulebook(version=1, services=[service(service_groups=["AFTERSALE"])])

    result = allocate(
        rulebook, shipment(accepted_service_groups=["AFTERSALE", "FEDEX"])
    )

    assert result.selected is not None


def test_service_in_no_accepted_group_is_excluded() -> None:
    rulebook = Rulebook(version=1, services=[service(service_groups=["PALLET"])])

    result = allocate(rulebook, shipment(accepted_service_groups=["AFTERSALE"]))

    assert result.selected is None
    failed = [c for c in result.service_results[0].checks if not c.ok]
    assert [c.name for c in failed] == ["service_group"]


def test_service_in_no_group_is_excluded_under_a_filter() -> None:
    # Allow-list, not wildcard (unlike proposition): a service with no declared
    # group is unreachable when the WMS sends an accepted set.
    rulebook = Rulebook(version=1, services=[service(service_groups=[])])

    result = allocate(rulebook, shipment(accepted_service_groups=["AFTERSALE"]))

    assert result.selected is None
    check = next(
        c for c in result.service_results[0].checks if c.name == "service_group"
    )
    assert check.ok is False


def test_empty_accepted_group_set_does_not_restrict() -> None:
    # The JSON path never sends groups: an empty accepted set is optimistic.
    rulebook = Rulebook(version=1, services=[service(service_groups=["AFTERSALE"])])

    result = allocate(rulebook, shipment(accepted_service_groups=[]))

    assert result.selected is not None
    check = next(
        c for c in result.service_results[0].checks if c.name == "service_group"
    )
    assert check.ok is True
    assert "optimistic" in check.actual
