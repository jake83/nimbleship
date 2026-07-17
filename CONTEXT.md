# NimbleShip

A carrier management system: the successor to the 3PL proxy. It sits between
sales/warehouse systems and delivery carriers, deciding how consignments ship,
producing labels and paperwork, and answering for every decision it makes.

## Language

**Legacy Interface**:
The WMS-facing SOAP/XML edge that speaks the MetaPack dialect so existing
callers (the WMS, Magento's current contract) work unchanged. A pure
translation skin over the same domain operations as the JSON API - it never
contains business logic.
_Avoid_: MetaPack integration (MetaPack the company is gone from this system;
only the dialect survives), SOAP API (as if it were a separate system)

**WMS**:
The warehouse management system (Solvitt) that requests consignments and
labels over the Legacy Interface. Carried over from the 3PL glossary.
_Avoid_: Solvitt (in code), warehouse system

**Consignment Code**:
The opaque handle the WMS uses to refer to one shipment across the Legacy
Interface's stateful lifecycle calls (create, allocate, paperwork); the
MetaPack dialect calls it the DMC code. The Legacy Interface maps it to the
order number, the domain-facing key. A Legacy Interface concept only - the
JSON API and domain core never see it.
_Avoid_: DMC code (in domain code), consignment ID (that is the domain row's
key), conflating it with the order number

**Warehouse**:
A logical dispatch identity, not (necessarily) a physical building - the
Metapack-style concept the company already works with. The WMS names a
Warehouse per order, which determines sender address and dispatch details;
websites are also mapped to Warehouses so delivery options can be configured
per site. Carries collection days and holidays, and its own timezone: the
dispatch day a Warehouse observes is its local day, so a Manifest declares the
local date of its creation, not the UTC date (a near-midnight scan-out would
otherwise be dated to the wrong day).
_Avoid_: assuming one warehouse, treating it as only a physical location,
dating dispatch in UTC rather than the Warehouse's local day

**Parcel Barcode**:
The scannable identity of one parcel, formed as the order number, a dash, and
the 1-based parcel sequence in label-print order (e.g. `95000254580-2`),
rendered as Code 128. Carried over from the 3PL glossary; named by ADR 0002
as a legacy contract obligation.
_Avoid_: parcel code, label barcode, 0-based sequences

**Parcels String**:
The comma-joined `{order number}-parcel-{n}:{barcode}` value returned to the
WMS in the paperwork response, used by every carrier that reports barcodes.
`{n}` is the 1-based print sequence, the same one embedded in the **Parcel
Barcode**. `{barcode}` is the carrier's own barcode for that parcel when it
reports one (e.g. a live carrier's tracking token); when the carrier reports
none - as Drop Out, which prints its own labels, does - it is the **Parcel
Barcode**, and then the two agree (`95000254580-parcel-2:95000254580-2`).
Carried over from the 3PL glossary.
_Avoid_: barcode list, 0-based `{n}`, assuming `{barcode}` is always the
Parcel Barcode

**SSCC** (Serial Shipping Container Code):
The GS1 18-digit identity of one shipping unit, printed as a GS1-128 barcode
and declared to the carrier. The carrier provisions a range (the leading
digits, held in Carrier Config); NimbleShip mints each SSCC by incrementing a
bounded suffix within that range and appending the GS1 mod-10 check digit.
Per-parcel, allocated at booking. When the suffix exhausts, the range is
spent and a new one must be requested from the carrier - suffixes never wrap,
which would reissue a live code. Stored as the parcel's carrier-facing
barcode.
_Avoid_: treating it as a self-generated **Parcel Barcode** (that is the
internal `{order}-{seq}` Code 128), or as carrier-issued at the
individual-number level (only the range is carrier-provisioned)

**Girth**:
The parcel size measure carriers price and restrict by: twice the height
plus twice the width plus the length, in centimetres. A shipment fact;
services may declare a maximum. Carried over from the old system's
allocation vocabulary.
_Avoid_: circumference, "size" (ambiguous with longest dimension)

**Shipping Area**:
A named geography (e.g. Scottish Highlands, Northern Ireland) defined by
postcode prefixes, used by services to declare where they do or do not
deliver. The mechanism is data (area and prefix tables); a shipment's areas
are resolved from its destination postcode at evaluation time.
_Avoid_: blocked area (that is one use of an area, not the concept),
postcode list (the area is the named thing, prefixes are its definition)

**Carrier Definition**:
The versioned, declarative document that tells the integration engine how
to book, label, manifest, and track with one carrier: auth scheme, mapping
entries (target field, source fact, transforms from a closed vocabulary),
response extraction, and named plugins for the exotic parts. Data on the
draft/test/publish rails - never code.
_Avoid_: carrier integration (when meaning the document), adapter/driver
(those imply code)

**Carrier Config**:
The per-install account facts for one carrier - credentials, endpoints,
account numbers - referenced by Carrier Definitions as config.* sources and
stored outside them: a definition describes HOW to talk to a carrier;
config holds WHO is talking. A fresh install is a deploy plus configuration.
_Avoid_: putting credentials in a Carrier Definition, "carrier settings"

**Golden Replay**:
The offline test gate for a draft Carrier Definition: its rendered requests
are diffed against recorded real traffic for historical shipments. Green
replay is required to publish; live sandbox calls are a separate, optional
tier. Staging caveat: until live traffic exists for a carrier, the baseline
is the active definition's re-render; recorded traffic becomes the baseline
as it accumulates.
_Avoid_: dry run (that is the rulebook's replay; this one renders requests)

**Delivery Cost**:
What a carrier charges the company to deliver a consignment. Used to pick the
cheapest suitable carrier during allocation.
_Avoid_: confusing with Delivery Charge

**Delivery Charge**:
What the company charges the customer for a delivery option at checkout.
_Avoid_: confusing with Delivery Cost

**Manifest**:
The per-carrier declaration of consignments that have physically left the
warehouse, sent to the carrier after the WMS confirms dispatch (today: the
"scan-out" at trailer-door close). For Dachser the manifest takes the form of
an EDI file; the format is per-carrier, the concept is universal.
_Avoid_: scan-out (warehouse-internal jargon for the trigger), EDI (that is
one carrier's file format, not the concept)

**Dispatched**:
A consignment has physically left the warehouse (LDG). The moment is set by
carrier type, not by which edge entered the order (ADR 0013): a manifest
carrier (Dachser) is dispatched when its **Manifest is sent**; every other
carrier is dispatched the moment **labels are returned** (there is no manifest
and no separate departure signal). Between allocation and dispatch a
manifest-carrier consignment passes through `ready_to_manifest` (the WMS marked
it ready, selectively) and `on_manifest` (a pending Manifest exists, not yet
sent).
_Avoid_: the packing-bench "dispatch" milestone (that is a workflow step, not
physical departure); marking dispatched at manifest creation rather than send

**Delivery Proposition**:
The customer-facing delivery promise: next day, next day pre-10, economy,
Saturday, and so on. Sold at checkout, honoured at dispatch: services declare
which propositions they fulfil, and dispatch selects only among services
fulfilling the proposition the customer bought. The single value a JSON API
caller states directly. Distinct from a Service Group: a proposition is a
promise, a group is an allow-list of carrier services; a service may declare
both, and the two filter independently.
_Avoid_: conflating with Service Group; shipping fee filter (the WMS-side
transport for it)

**Service Group**:
A named allow-list of carrier services a legacy order is permitted to use,
carried through from the WMS dialect (`custom1`, the requested group, unioned
with `acceptableCarrierServiceGroupCodes`, the accepted set). An eligibility
filter, not a promise: dispatch returns only services declared members of an
accepted group, so a service in no group is unreachable under a filter (not a
wildcard). Services declare their memberships in the rulebook; the catalogue
of codes is company data, adopted verbatim from the WMS (no remap). Only the
Legacy Interface sends them - the JSON API filters by Delivery Proposition
instead. An empty accepted set does not restrict (a JSON order is unaffected);
the Legacy Interface faults a legacy order that carries no group at all, and
faults an accepted code absent from the catalogue.
_Avoid_: treating it as a Delivery Proposition or "the promise half"; a
no-group service as a wildcard; remapping the WMS codes

**Constraint**:
A named, authored statement in the eligibility rulebook: a scope (which
services), a condition over shipment/origin facts, and a single effect -
block. All matching constraints apply; no ordering, no precedence. The unit
of rule authoring, review, testing, and explanation.
_Avoid_: allocation rule group, return value

**Order Origin**:
An explicit structured fact about where an order came from: platform (Magento
2, the legacy marketplace platform, the new in-house platform), website/brand,
and marketplace (ManoMano, Amazon, eBay) where applicable. Stated directly by
JSON API callers; derived by the Legacy Interface from old signals (order ID
prefix/length, recipient email domain) via per-caller translation config.
_Avoid_: m2Order, marketplacesOrder, orderIdPrefix, "starts with 7" - the
inference hacks these facts replace

**Customs Identity**:
The deemed supplier whose IOSS number, VAT number, EORI, and company details
go on customs paperwork for an international order. The company's own
registrations for direct sales; the marketplace's for marketplace orders
(e.g. ManoMano under EU deemed-supplier rules).
_Avoid_: treating ManoMano as a one-off special case, "the IOSS number" (as
if there were only one)

**Tracking Event**:
A carrier-agnostic record of a consignment's progress. Voila is currently the
main source (a webhook aggregator), but the structure is generic: direct
carrier integrations feed the same shape. Moving carriers off Voila is out of
scope for NimbleShip; supporting both source types is in scope.
_Avoid_: Voila tracking event (as the generic name), naming Voila outside the
Voila adapter

## Relationships

- The **WMS** talks only to the **Legacy Interface**; everything else talks to
  the JSON API. Both edges call the same domain operations.
- The **Legacy Interface** speaks a stateful lifecycle (create -> allocate ->
  paperwork per **Consignment Code**); it mocks the first two and runs the one
  atomic domain create-consignment at paperwork (ADR 0011). The JSON API does
  the same work in a single call.

## Flagged ambiguities

- **Service Group**: resolved (ADR 0012) - see the Language entry above. Not the
  "split into a proposition plus order-type facts" the Session A note first
  assumed: the audit showed a group is a carrier-service allow-list, carried
  through as its own eligibility axis with no mapping table.
- Legacy sentinel zeros: checkout requests send unknown numerics as 0
  (consignmentValue, maxDimension). The Legacy Interface must translate
  these to absent facts, never the number zero.
- "Scan-out" (warehouse jargon) vs **Manifest**: scan-out is the WMS-side
  trigger (trailer doors close, consignment list sent over); the Manifest is
  what NimbleShip sends the carrier as a result. The old system blurs these.
