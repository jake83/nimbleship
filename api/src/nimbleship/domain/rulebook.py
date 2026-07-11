from typing import cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.domain.allocation import Rulebook, ServiceDeclaration
from nimbleship.models import RulebookVersion

# Demo seed for fresh installs: two generic Drop Out services proving the
# weight-band and country declarations plus cheapest-cost selection.
# Real installs replace this via the rules workflow - never in code.
_DEMO_SERVICES: list[dict[str, object]] = [
    {
        "code": "DROPOUT-STD",
        "carrier": "dropout",
        "name": "Drop Out Standard",
        "weight_min_kg": "0",
        "weight_max_kg": "30",
        "countries": ["GB"],
        "cost": "4.50",
        "tie_break_order": 1,
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
]


def active_rulebook(session: Session) -> Rulebook:
    """The highest published rulebook version; seeds the demo rulebook on a
    fresh install."""
    row = session.execute(
        select(RulebookVersion)
        .where(RulebookVersion.status == "published")
        .order_by(RulebookVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()

    if row is None:
        row = RulebookVersion(
            status="published",
            author="seed",
            data={"services": _DEMO_SERVICES},
        )
        session.add(row)
        session.flush()

    declared = cast(list[dict[str, object]], row.data["services"])
    services = [ServiceDeclaration.model_validate(service) for service in declared]
    return Rulebook(version=row.version, services=services)
