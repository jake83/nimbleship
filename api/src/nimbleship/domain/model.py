"""The rulebook data model (ADRs 0007, 0008).

Everything a user decides lives here as data; the checks package holds the
interpreters. Fields added for Phase 2 default to "unrestricted" so rulebook
versions stored before they existed still validate.
"""

from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from nimbleship.domain.charges import ChargeBand
from nimbleship.domain.costs import CostBand


class ServiceDeclaration(BaseModel):
    code: str
    carrier: str
    name: str
    weight_min_kg: Decimal
    weight_max_kg: Decimal
    countries: list[str]
    cost: Decimal
    tie_break_order: int

    # Phase 2 declarations; None/[] = unrestricted (legacy-compatible).
    max_dimension_cm: Decimal | None = None
    max_girth_cm: Decimal | None = None
    # Shipping area codes (chunk A defines matching); served None = anywhere
    # within the allowed countries.
    areas_served: list[str] | None = None
    areas_blocked: list[str] = []
    # Delivery Proposition codes this service fulfils (chunk B defines
    # semantics); [] = unrestricted.
    propositions: list[str] = []
    # Banded Delivery Cost/Charge structures (chunks C and D implement the
    # calculators); None = flat `cost`, no charges configured.
    cost_bands: list[CostBand] | None = None
    charge_bands: list[ChargeBand] | None = None


class Rulebook(BaseModel):
    version: int
    # Non-empty: a live rulebook with zero services would silently reject
    # every order - a total allocation outage behind 200s (refuter, PR #9).
    services: list[ServiceDeclaration] = Field(min_length=1)

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
    """The facts. Optional facts may be unknown at checkout time; checks
    treat unknown as optimistically eligible (ADR 0007) - dispatch
    re-evaluates when the facts are complete."""

    order_number: str
    destination_country: str
    total_weight_kg: Decimal
    parcel_count: int

    max_dimension_cm: Decimal | None = None
    max_girth_cm: Decimal | None = None
    value: Decimal | None = None
    # The Delivery Proposition the customer bought; None = no filter.
    proposition: str | None = None
    # Shipping area codes matched from the destination (chunk A computes).
    shipping_areas: list[str] = []


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
    # The Delivery Cost the winner was selected on: calculated from its
    # cost bands when present, else its flat cost. None when nothing was
    # selected. `selected.cost` alone would misreport banded services.
    selected_cost: Decimal | None = None
    reason: str
