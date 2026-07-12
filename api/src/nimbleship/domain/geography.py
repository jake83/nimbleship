"""Shipping Area resolution: destination postcode + country -> area codes.

The areas a shipment is in are facts resolved BEFORE the pure evaluation
(ADR 0008 addendum), so allocate() stays a pure function. Matching is
longest-prefix: a more specific prefix (IV1) overrides a general one (IV).
The lookup ports the old system's getBlockedHauliersForPostcode
optimisation - one query against every prefix of the postcode rather than
a scan of the whole table."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.models import PostcodeArea, ShippingArea


def resolve_shipping_areas(session: Session, postcode: str, country: str) -> list[str]:
    """Area codes whose longest postcode prefix matches, sorted for stable
    traces; empty when no prefix matches (checks treat that optimistically)."""
    normalised = postcode.strip().upper()
    if not normalised:
        return []
    prefixes = [normalised[:length] for length in range(len(normalised), 0, -1)]
    rows = session.execute(
        select(PostcodeArea.prefix, ShippingArea.code)
        .join(ShippingArea, PostcodeArea.area_id == ShippingArea.id)
        .where(PostcodeArea.prefix.in_(prefixes))
        .where(ShippingArea.country == country.strip().upper())
    ).all()
    if not rows:
        return []
    longest = max(len(prefix) for prefix, _ in rows)
    return sorted({code for prefix, code in rows if len(prefix) == longest})
