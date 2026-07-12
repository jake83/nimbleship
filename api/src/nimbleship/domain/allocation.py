"""The eligibility engine and selection policy (ADRs 0007, 0008).

The data model lives in domain/model.py; the check interpreters live in
domain/checks/, one module per declaration kind. This module re-exports the
model names so callers keep one import point for allocation concerns.
"""

from nimbleship.domain.checks import ALL_CHECKS
from nimbleship.domain.model import (
    AllocationResult,
    Check,
    Rulebook,
    ServiceDeclaration,
    ServiceResult,
    Shipment,
)

__all__ = [
    "AllocationResult",
    "Check",
    "Rulebook",
    "ServiceDeclaration",
    "ServiceResult",
    "Shipment",
    "allocate",
]


def _evaluate_service(service: ServiceDeclaration, shipment: Shipment) -> ServiceResult:
    checks = [check.evaluate(service, shipment) for check in ALL_CHECKS]
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
