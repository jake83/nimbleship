"""The checkout-facing quote projection (ADR 0007's checkout moment): which
services are eligible for a shipment-shaped payload, priced with Delivery
Charges. The seed of the checkout endpoint - the integration step shapes the
final contract."""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from nimbleship.db import get_session
from nimbleship.domain.allocation import Shipment, allocate
from nimbleship.domain.charges import calculate_charge
from nimbleship.domain.rulebook import active_rulebook

router = APIRouter(prefix="/quotes", tags=["quotes"])

SessionDep = Annotated[Session, Depends(get_session)]


class QuotedServiceOut(BaseModel):
    code: str
    carrier: str
    name: str
    # None = no Delivery Charge configured for this service/destination/
    # weight - shown honestly, never as the old system's 0.0 sentinel.
    charge: Decimal | None


class QuoteOut(BaseModel):
    rulebook_version: int
    services: list[QuotedServiceOut]


@router.post("")
def quote(shipment: Shipment, session: SessionDep) -> QuoteOut:
    """Evaluate the active rulebook on checkout-time facts (unknown facts
    are optimistic, per ADR 0007) and price each eligible service."""
    rulebook = active_rulebook(session)
    result = allocate(rulebook, shipment)
    eligible_codes = {r.service_code for r in result.service_results if r.eligible}

    services = []
    for service in rulebook.services:
        if service.code not in eligible_codes:
            continue
        charge = (
            calculate_charge(service.charge_bands, shipment, shipment.shipping_areas)
            if service.charge_bands is not None
            else None
        )
        services.append(
            QuotedServiceOut(
                code=service.code,
                carrier=service.carrier,
                name=service.name,
                charge=charge,
            )
        )

    return QuoteOut(rulebook_version=rulebook.version, services=services)
