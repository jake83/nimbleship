"""The eligibility engine and selection policy (ADRs 0007, 0008).

The data model lives in domain/model.py; the check interpreters live in
domain/checks/, one module per declaration kind. This module re-exports the
model names so callers keep one import point for allocation concerns.
"""

from decimal import Decimal

from nimbleship.domain.checks import ALL_CHECKS
from nimbleship.domain.costs import calculate_cost
from nimbleship.domain.model import (
    AllocationResult,
    Check,
    Rulebook,
    ServiceDeclaration,
    ServiceResult,
    Shipment,
    duplicate_service_field,
)

__all__ = [
    "AllocationResult",
    "Check",
    "Rulebook",
    "ServiceDeclaration",
    "ServiceResult",
    "Shipment",
    "allocate",
    "duplicate_service_field",
]


def _evaluate_service(service: ServiceDeclaration, shipment: Shipment) -> ServiceResult:
    checks = [check.evaluate(service, shipment) for check in ALL_CHECKS]
    return ServiceResult(
        service_code=service.code,
        eligible=all(check.ok for check in checks),
        checks=checks,
    )


def selection_cost(service: ServiceDeclaration, shipment: Shipment) -> Decimal | None:
    """The Delivery Cost the selection policy compares: calculated from the
    service's cost bands when it has any, else the flat cost (the migration
    path for services whose banded costs are not configured yet)."""
    if service.cost_bands is None:
        return service.cost
    return calculate_cost(service.cost_bands, shipment)


_NO_COST_DATA = Check(
    name="no-cost-data",
    ok=False,
    expected="an applicable cost band",
    actual="no cost data",
)


def allocate(rulebook: Rulebook, shipment: Shipment) -> AllocationResult:
    service_results = [
        _evaluate_service(service, shipment) for service in rulebook.services
    ]
    results_by_code = {r.service_code: r for r in service_results}
    eligible = [s for s in rulebook.services if results_by_code[s.code].eligible]

    # ADR 0007/0008: a service with cost bands but no applicable band cannot
    # be costed - it is excluded LOUDLY, readable in the trace, never
    # silently skipped.
    costed: list[tuple[Decimal, ServiceDeclaration]] = []
    for service in eligible:
        cost = selection_cost(service, shipment)
        if cost is None:
            result = results_by_code[service.code]
            result.checks.append(_NO_COST_DATA.model_copy())
            result.eligible = False
        else:
            costed.append((cost, service))

    if not costed:
        reason = (
            "no eligible services"
            if not eligible
            else "no cost data for any eligible service"
        )
        return AllocationResult(
            rulebook_version=rulebook.version,
            service_results=service_results,
            selected=None,
            reason=reason,
        )

    winning_cost, winner = min(costed, key=lambda c: (c[0], c[1].tie_break_order))
    return AllocationResult(
        rulebook_version=rulebook.version,
        service_results=service_results,
        selected=winner,
        selected_cost=winning_cost,
        reason="cheapest eligible service",
    )
