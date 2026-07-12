"""Declaration check registry (ADR 0008 addendum).

Each module here interprets one KIND of question a service declaration can
pose; the values it compares live in the rulebook, never in code. Adding a
declaration kind = one module implementing DeclarationCheck + one entry in
ALL_CHECKS + its own tests. Checks must handle unknown shipment facts by
reporting ok=True with actual="unknown (optimistic)" - see ADR 0007 and
checks/dimension.py for the pattern."""

from typing import Protocol

from nimbleship.domain.checks.area_blocked import AreaBlockedCheck
from nimbleship.domain.checks.area_served import AreaServedCheck
from nimbleship.domain.checks.country import CountryCheck
from nimbleship.domain.checks.dimension import DimensionCheck
from nimbleship.domain.checks.proposition import PropositionCheck
from nimbleship.domain.checks.weight import WeightBandCheck
from nimbleship.domain.model import Check, ServiceDeclaration, Shipment


class DeclarationCheck(Protocol):
    name: str

    def evaluate(self, service: ServiceDeclaration, shipment: Shipment) -> Check: ...


ALL_CHECKS: tuple[DeclarationCheck, ...] = (
    CountryCheck(),
    WeightBandCheck(),
    DimensionCheck(),
    PropositionCheck(),
    AreaBlockedCheck(),
    AreaServedCheck(),
)
