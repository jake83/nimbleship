# 11. Legacy edge: mock the stateful lifecycle, defer real work to paperwork

Date: 2026-07-15

## Status

Accepted

## Context

ADR 0002 fixed the shape: one domain core, two protocol edges, the WMS-facing
SOAP dialect a logic-free translation skin over the same domain operations the
JSON API uses. Phase 4 builds that edge. Mapping the dialect onto the core
surfaces one hard mismatch.

The MetaPack dialect the WMS speaks is a **stateful, multi-call lifecycle** for
one shipment:

1. `createConsignments` - the WMS registers an order; the edge returns a
   consignment code and status `Unallocated`.
2. `allocateConsignments` - the WMS asks to assign a carrier; the edge returns
   status `Allocated` and a carrier.
3. `createPaperworkForConsignments` - the WMS asks for labels; the edge returns
   base64 label PDFs, a tracking reference, and the parcels string.

NimbleShip's domain `create-consignment` operation is **atomic**: it allocates,
books the carrier, and produces the label in one call. Three stateful calls do
not line up with one atomic operation.

The old proxy's behaviour, confirmed against its code, resolves it: it does
**no real carrier work** in the first two calls. `createConsignments` is pure
data intake returning a synthetic response; `allocateConsignments` selects a
carrier and records the choice but never books (it even echoes the metapack
code back as the carrier code); only `createPaperworkForConsignments` makes a
real carrier call, gets the label and tracking, and emits the parcels string.
The first two are effectively mocked. The old proxy is also **strictly
ordered** - each step reads the prior call's stored request, and an out-of-order
call produces an incomplete response.

## Decision

The legacy edge mocks the first two lifecycle calls and defers all real work to
paperwork:

- `createConsignments` and `allocateConsignments` return synthetic,
  dialect-valid responses and **accumulate** the shipment data into a staging
  record. Neither calls the domain core.
- `createPaperworkForConsignments` runs the atomic domain `create-consignment`
  operation (allocate + book + label) against the accumulated data, then
  translates the result back into the paperwork response's legacy obligations
  (base64 labels, tracking reference, parcels string).

The domain core stays atomic and SOAP-free; the staging is translation
bookkeeping, not business logic, so the edge stays logic-free per ADR 0002.

NimbleShip **mints the consignment code** - an iterable `NS`-prefixed handle,
not a MetaPack-style DMC code - at `createConsignments` and echoes it back; the
WMS reuses it on allocate and paperwork. Because the code only exists once
create has run, the WMS cannot reference it earlier, so `createConsignments`
structurally precedes `allocateConsignments` and the "allocate arrives first"
worry dissolves: there is no valid code to allocate against until create has
minted one. This matches the old proxy's real ordering without inheriting its
brittleness - the ordering is enforced by the identity model, not assumed.

The atomic `create-consignment` orchestration - today inline in the JSON
consignments router - is extracted into a shared domain service so both edges
invoke the same operation, as ADR 0002 requires.

## Consequences

- The WMS sees the exact lifecycle it expects; the edge holds only staging
  state between calls, keyed by the minted consignment code (with the order
  number as the domain-facing key).
- One real carrier interaction per shipment, at paperwork, matching the old
  proxy - so a shadow-mode run books once, not three times.
- The consignment code is NimbleShip-native and iterable; a future
  ConsignmentSearchService can look a shipment up by it, so the code outlives
  staging by being carried onto the domain Consignment at paperwork (not yet
  built - staging holds it for now).
- Cost: a staging model and its lifecycle (creation at create, consumption at
  paperwork, pruning of abandoned records) that the atomic JSON path does not
  need. Staging holds unvalidated inbound data briefly; it is ephemeral and
  never the system of record.
