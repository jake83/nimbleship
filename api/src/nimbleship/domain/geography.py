"""Shipping Area resolution: destination postcode + country -> area codes.

The areas a shipment is in are facts resolved BEFORE the pure evaluation
(ADR 0008 addendum), so allocate() stays a pure function. Every matching
prefix counts (old-system parity): IV1 2AB is in the IV-defined area AND
the IV1-defined one - a specific prefix never shadows a general one, so
area definitions stay independent of each other. "Longest prefix" survives
only as the query optimisation (one query against every prefix of the
postcode, ported from the old getBlockedHauliersForPostcode)."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.models import PostcodeArea, ShippingArea


def resolve_shipping_areas(session: Session, postcode: str, country: str) -> list[str]:
    """Area codes for every matching postcode prefix, sorted for stable
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
    return sorted({code for _, code in rows})
