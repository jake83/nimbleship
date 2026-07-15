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
- **PR4: createPaperworkForConsignments.** The real work + the ADR-named
  obligations: base64 labels, tracking reference, parcels string.
- **PR5: manifest + dispatch** (markAsReadyToManifest, createManifest) onto the
  existing dispatch-confirmation/manifest domain path.
- **PR6+: shadow mode.** Replay recorded traffic through the edge, diff
  allocations/labels/paperwork against the incumbent (reuse the golden-replay
  diff machinery).
- **Later: WSDL serving** - deferred from PR1 (the WMS runs off a fixed cached
  WSDL; NimbleShip serving it is self-description, best built once all
  operations exist).

## Open questions (grilling agenda)

Batched for a session; none block PR1-3, each is needed by the PR noted.

1. **serviceGroup -> Delivery Proposition mapping** (PR3/PR4): the per-value
   table from incoming `custom1`/serviceGroup codes to a proposition +
   order-type facts (CONTEXT.md flags a ~40-row table). Domain knowledge only
   the old system/owner has.
2. **Order Origin derivation rules** (PR3/PR4): the per-caller config mapping
   old signals (order-id prefix/length, recipient email domain) to
   platform/website/marketplace facts (CONTEXT.md: Order Origin).
3. **Sentinel-zero fields** (PR3/PR4): the full set of numeric fields the WMS
   sends as `0` meaning absent (consignmentValue, maxDimension confirmed - are
   there others?). Translated to absent facts, never the number zero.
4. **Paperwork response fidelity** (PR4): confirm the exact `<parcels>` /
   `<trackingReference>` / `<labels>` positions and the Drop Out tracking-omit
   rule against a real paperwork response; byte-match matters here (the WMS
   parses positionally).
5. **createConsignments response fidelity** (any): PR1 emits a lean response
   (code, orderNumber, status, parcelCount). Confirm whether the live WMS needs
   more of the echoed Consignment than that.
