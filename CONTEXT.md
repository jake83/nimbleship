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

- **Service Group** is overloaded: primarily it encodes the delivery
  proposition the customer bought (next day, pre-10, economy) and flows from
  the sales platform through the WMS into carrier selection, but it is also
  believed to influence country eligibility "somehow". Needs unpicking in the
  allocation/delivery-options design session before it gets a canonical
  definition - "Delivery Proposition" is a candidate name for the main use.
- "Scan-out" (warehouse jargon) vs **Manifest**: scan-out is the WMS-side
  trigger (trailer doors close, consignment list sent over); the Manifest is
  what NimbleShip sends the carrier as a result. The old system blurs these.
