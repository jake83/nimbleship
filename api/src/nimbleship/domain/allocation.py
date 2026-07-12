"""The eligibility rulebook and selection policy (ADRs 0007, 0008).

Phase 1 vocabulary: service declarations only (weight band, allowed
countries). Named block-constraints arrive with the unified model in
Phase 2; the structures here already leave room for them.
"""

from decimal import Decimal

from pydantic import BaseModel, model_validator


class ServiceDeclaration(BaseModel):
    code: str
    carrier: str
    name: str
    weight_min_kg: Decimal
    weight_max_kg: Decimal
    countries: list[str]
    cost: Decimal
    tie_break_order: int


class Rulebook(BaseModel):
    version: int
    services: list[ServiceDeclaration]

    @model_validator(mode="after")
    def _codes_and_tie_breaks_are_unique(self) -> "Rulebook":
        """Selection must be order-blind: same rulebook version, same
        shipment, same answer, always. Duplicate codes would make winner
        lookup ambiguous; duplicate tie-break orders would let JSON order
        decide a cost tie."""
        seen_codes: set[str] = set()
        seen_orders: set[int] = set()
        for service in self.services:
            if service.code in seen_codes:
                raise ValueError(f"duplicate service code: {service.code}")
            if service.tie_break_order in seen_orders:
                raise ValueError(
                    f"duplicate tie-break order: {service.tie_break_order}"
                )
            seen_codes.add(service.code)
            seen_orders.add(service.tie_break_order)
        return self


class Shipment(BaseModel):
    order_number: str
    destination_country: str
    total_weight_kg: Decimal
    parcel_count: int


class Check(BaseModel):
    name: str
    ok: bool
    expected: str
    actual: str


class ServiceResult(BaseModel):
    service_code: str
    eligible: bool
    checks: list[Check]


class AllocationResult(BaseModel):
    rulebook_version: int
    service_results: list[ServiceResult]
    selected: ServiceDeclaration | None
    reason: str


def _evaluate_service(service: ServiceDeclaration, shipment: Shipment) -> ServiceResult:
    checks = [
        Check(
            name="country",
            ok=shipment.destination_country in service.countries,
            expected=f"one of {', '.join(service.countries)}",
            actual=shipment.destination_country,
        ),
        Check(
            name="weight",
            ok=service.weight_min_kg
            <= shipment.total_weight_kg
            <= service.weight_max_kg,
            expected=f"{service.weight_min_kg}kg to {service.weight_max_kg}kg",
            actual=f"{shipment.total_weight_kg}kg",
        ),
    ]
    return ServiceResult(
        service_code=service.code,
        eligible=all(check.ok for check in checks),
        checks=checks,
    )


def allocate(rulebook: Rulebook, shipment: Shipment) -> AllocationResult:
    service_results = [
        _evaluate_service(service, shipment) for service in rulebook.services
    ]
    eligible_codes = {r.service_code for r in service_results if r.eligible}
    eligible = [s for s in rulebook.services if s.code in eligible_codes]

    if not eligible:
        return AllocationResult(
            rulebook_version=rulebook.version,
            service_results=service_results,
            selected=None,
            reason="no eligible services",
        )

    winner = min(eligible, key=lambda s: (s.cost, s.tie_break_order))
    return AllocationResult(
        rulebook_version=rulebook.version,
        service_results=service_results,
        selected=winner,
        reason="cheapest eligible service",
    )
