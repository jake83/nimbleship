# 2. JSON core with a legacy SOAP edge

Date: 2026-07-11

## Status

Accepted

## Context

NimbleShip replaces a proxy whose callers it does not control. The WMS
(Solvitt) speaks a MetaPack-derived SOAP/XML dialect that is effectively
permanent, and Magento calls the system for delivery options. For NimbleShip
to ever replace the incumbent 3PL system at the workplace, it must speak that
dialect. At the same time, building a new system around a 2000s SOAP dialect
would poison a greenfield domain model with eight years of MetaPack-isms.

Alternatives considered:

- Drop-in SOAP compatibility as the primary interface, built first.
- Modern JSON API only, betting that the WMS integration gets rewritten.
- Modern JSON API as the default interface, with the SOAP dialect offered as a
  legacy interface translating onto the same core.

## Decision

NimbleShip has one domain core and two protocol edges:

- The default, first-class interface is a modern JSON/REST API (FastAPI
  native, typed, documented). All new integrations use it.
- A legacy interface speaks the WMS SOAP/XML dialect so existing callers
  (Solvitt, and Magento's current contract) work unchanged.

The legacy edge is a pure translation skin: it maps XML to the same domain
operations the JSON API uses and back. It contains no business logic, no
validation policy of its own, and no decision-making. One brain, two mouths.

The domain model is designed first, then verified against the legacy
contract's obligations (parcels strings, parcel barcodes, base64 labels in
paperwork responses) - the SOAP shapes never leak inward.

## Consequences

- Solvitt and Magento can adopt NimbleShip with zero changes on their side.
- The JSON API stays clean for demos, the portfolio story, and future callers.
- Retiring the legacy edge one day means deleting an adapter, not surgery.
- Cost: the adapter must be maintained and contract-tested against real
  recorded WMS traffic, and the domain core carries the obligation to satisfy
  legacy semantics even where a fresh design might not have included them.

## Clarification: what "no validation policy" means (2026-07-17)

"No validation policy of its own" means no business or decision policy. Field
and shape invariants of a Consignment - length limits, at least one parcel, a
parcel weight that fits its column - are domain facts, so the domain core
(`create_consignment`) owns them and raises `ConsignmentError`; each edge maps
that to its own error shape (the JSON API to an HTTPException, the legacy edge
to a SOAP fault). Every such limit is a single shared constant, referenced by
the column, the JSON pydantic bound, and the domain check, so the three cannot
drift. The edge still performs the structural gatekeeping its own translation
requires - a batch item with no key or a duplicate key, malformed XML, and the
one length check on `order_number` that protects the pre-domain staging write
(its indexed key). That is translation feasibility and bookkeeping integrity,
not business policy.
