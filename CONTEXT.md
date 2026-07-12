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

**Warehouse**:
A logical dispatch identity, not (necessarily) a physical building - the
Metapack-style concept the company already works with. The WMS names a
Warehouse per order, which determines sender address and dispatch details;
websites are also mapped to Warehouses so delivery options can be configured
per site. Carries collection days and holidays.
_Avoid_: assuming one warehouse, treating it as only a physical location

**Parcel Barcode**:
The scannable identity of one parcel, formed as the order number, a dash, and
the 1-based parcel sequence in label-print order (e.g. `95000254580-2`),
rendered as Code 128. Carried over from the 3PL glossary; named by ADR 0002
as a legacy contract obligation.
_Avoid_: parcel code, label barcode, 0-based sequences

**Parcels String**:
The comma-joined `{order number}-parcel-{n}:{barcode}` value returned to the
WMS in the paperwork response, used by every carrier that reports barcodes.
`{n}` is the same 1-based print sequence embedded in the **Parcel Barcode**;
the two must always agree (`95000254580-parcel-2:95000254580-2`). Carried
over from the 3PL glossary.
_Avoid_: barcode list, 0-based `{n}`

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

**Delivery Proposition**:
The customer-facing delivery promise: next day, next day pre-10, economy,
Saturday, and so on. Sold at checkout, honoured at dispatch: services declare
which propositions they fulfil, and dispatch selects only among services
fulfilling the proposition the customer bought. Replaces the "promise" half
of the old service groups; the order-type half (MARKETPLACE, AFTERSALE)
becomes order-type facts instead.
_Avoid_: service group (the old overloaded term), shipping fee filter (the
WMS-side transport for it)

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

## Flagged ambiguities

- **Service Group** (resolved in Session A): the old concept split in two -
  **Delivery Proposition** for the customer promise, order-type facts
  (marketplace, aftersale) for the rest. The legacy edge carries a per-value
  mapping table from incoming serviceGroup codes to proposition + facts;
  building that 40-row table during migration is the forcing function that
  documents what each code meant.
- Legacy sentinel zeros: checkout requests send unknown numerics as 0
  (consignmentValue, maxDimension). The Legacy Interface must translate
  these to absent facts, never the number zero.
- "Scan-out" (warehouse jargon) vs **Manifest**: scan-out is the WMS-side
  trigger (trailer doors close, consignment list sent over); the Manifest is
  what NimbleShip sends the carrier as a result. The old system blurs these.
