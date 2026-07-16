# Phase 4 - Legacy edge and shadow mode

Goal (ROADMAP): drop-in credibility. The WMS-facing SOAP dialect as a pure
translation skin (ADR 0002), plus shadow mode to diff NimbleShip's output
against the incumbent's on recorded production traffic.

## The dialect (from the 3PL proxy audit)

SOAP 1.1, MetaPack "DeliveryManager" namespace (`urn:DeliveryManager/services`,
`urn:DeliveryManager/types`), HTTP Basic Auth, three services:

- **ConsignmentService**: createConsignments, updateConsignments,
  createPaperworkForConsignments, markConsignmentsAsPrinted,
  markConsignmentsAsReadyToManifest, deleteConsignment.
- **AllocationService**: findDeliveryOptions, allocateConsignments, deallocate,
  verifyAllocation.
- **ManifestService**: createManifest, markConsignmentsAsReadyToManifest.

Two hard parts: **multiref encoding** (complex values are id-tagged siblings
under the Body referenced by `href="#id"`) and **custom extensions** in the
paperwork response (`<parcels>` string, `<trackingReference>`, base64
`<labels>`) that are not in the stock WSDL. 31 real request/response fixtures
exist in the old system; they become the contract-test corpus as each operation
lands (PR1 uses a synthetic, MetaPack-shaped fixture, not one of the 31).

## The lifecycle bridge (ADR 0011)

MetaPack is a stateful three-call lifecycle; the domain create-consignment is
atomic. The edge **mocks create and allocate** (synthetic responses that stage
data) and runs the real domain work at **paperwork**. NimbleShip mints an
iterable `NS`-style consignment code at create and echoes it back, so create
structurally precedes allocate. Confirmed against the old proxy's real
behaviour: it does the same (real carrier work only at paperwork).

## Chunk plan

- **PR1 (this): edge skeleton + createConsignments.** SOAP router outside /api,
  Basic Auth, multiref parse (defusedxml) / build (stdlib ET), SOAP faults, the
  staging model + NS-code minting, createConsignments (stage + synthetic
  Unallocated response). Contract-tested against a fixture. ADR 0011 + CONTEXT
  terms.
- **PR2: allocateConsignments.** Stage the carrier choice; synthetic Allocated
  response. Resolve findDeliveryOptions if the WMS uses it.
- **PR3: extract the shared domain create-consignment service.** The JSON
  router's inline orchestration (allocate + book + label + events) becomes one
  domain function both edges call (ADR 0002).
- **PR4a: createPaperworkForConsignments - the lifecycle bridge.** Consume the
  staged create+allocate data, run the atomic domain create-consignment, and
  return the ADR-named obligations: base64 label PDF, tracking reference (when
  the carrier reports one), and the Parcels String (CONTEXT.md). Contract-tested
  against a synthetic request; DropOut end to end. Deliberately deferred to PR4b
  (they need the grilling session's domain knowledge): the serviceGroup ->
  Delivery Proposition mapping (so dispatch runs unfiltered, proposition=None, for
  now), Order Origin derivation, the full sentinel-zero field set, and
  byte-exact response fidelity. Scope guard: PR4a handles one consignmentCode
  per call (matching the real recorded single-Paperwork response) and refuses a
  batch up front - create_consignment commits the request session on its own
  failure paths, so a second code booking after a first would strand the first's
  real carrier booking behind the blanket fault the second raises; safe batching
  needs a partial-success response and per-code commit isolation, deferred with
  the response-fidelity work to PR4b. Known limitations carried to PR4b/PR5: a
  re-sent paperwork call faults on the duplicate-order 409 (no reprint path yet);
  and a rejected shipment faults without leaving a domain Consignment behind.
- **PR4b-1: paperwork response fidelity.** Return the WMS's real single
  Paperwork shape (documents/labels/trackingReference/parcels, positional) in
  place of PR4a's guessed Item array; the Parcels String carries the carrier's
  own barcode when it reports one, else the Parcel Barcode (Drop Out). Grounded
  in the old proxy's response template + a recorded example; the SOAP-encoding
  type decorations byte-match at shadow mode (PR6), which needs the live WMS.
  No owner input required.
- **PR4b-2: the translation mappings** (grilling-gated, open questions 1-3).
  The serviceGroup -> Delivery Proposition table (~39 rows), Order Origin
  derivation, and the sentinel-zero field set are company-specific data and
  per-caller config (never code constants), so they need a grilling session to
  settle the config schema and confirm the values before landing.
- **PR5: manifest + dispatch** (markAsReadyToManifest, createManifest) onto the
  existing dispatch-confirmation/manifest domain path.
- **PR6+: shadow mode.** Replay recorded traffic through the edge, diff
  allocations/labels/paperwork against the incumbent (reuse the golden-replay
  diff machinery).
- **Later: WSDL serving** - deferred from PR1 (the WMS runs off a fixed cached
  WSDL; NimbleShip serving it is self-description, best built once all
  operations exist).

## Open questions (grilling agenda)

Batched for a session; none block PR1-3 or the PR4a bridge, each is needed by
the PR noted.

1. **serviceGroup -> Delivery Proposition mapping** (PR4b-2): the per-value
   table from incoming `custom1`/serviceGroup codes to a proposition +
   order-type facts (CONTEXT.md flags a ~40-row table). Domain knowledge only
   the old system/owner has. PR4a runs dispatch unfiltered (proposition=None)
   until this lands.
2. **Order Origin derivation rules** (PR4b-2): the per-caller config mapping
   old signals (order-id prefix/length, recipient email domain) to
   platform/website/marketplace facts (CONTEXT.md: Order Origin).
3. **Sentinel-zero fields** (PR4b-2): the full set of numeric fields the WMS
   sends as `0` meaning absent (consignmentValue, maxDimension confirmed - are
   there others?). Translated to absent facts, never the number zero.
4. **Paperwork response fidelity** - the element structure (single Paperwork:
   `documents`/`labels`/positional `trackingReference`/`parcels`) and the Drop
   Out tracking-omit rule are settled in PR4b-1, grounded in the old proxy's
   response template and a recorded example. What remains for shadow mode (PR6):
   byte-match of the SOAP-encoding type decorations against the live WMS.
5. **createConsignments response fidelity** (any): PR1 emits a lean response
   (code, orderNumber, status, parcelCount). Confirm whether the live WMS needs
   more of the echoed Consignment than that.
