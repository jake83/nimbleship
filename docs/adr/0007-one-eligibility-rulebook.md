# 7. One eligibility rulebook for checkout and dispatch

Date: 2026-07-11

## Status

Accepted

## Context

In the old 3PL system, "how can this ship?" is encoded in four disconnected
places: allocation rule groups, delivery options config (HaulierService),
delivery charges, and delivery costs. Magento's checkout call consults only
delivery options + charges; the WMS dispatch call consults only allocation
rules + carrier-specific vetoes + costs. Service groups filter one path and
not the other. The deep-dive found six concrete divergence paths where
checkout can promise what dispatch will refuse (or refuse what dispatch would
allow), and nobody can see it coming - a class of bug users experience as
"why did my order not ship the way I chose?".

Metapack-style systems avoid this by construction: one eligibility model,
consulted at every moment that asks.

## Decision

NimbleShip has one eligibility rulebook answering one question: "which
carrier services are eligible for this shipment?" It is consulted at two
moments with different knowledge and different projections:

- **Checkout** (sales platform): evaluates eligibility on what is known at
  basket time, groups the eligible services by Delivery Proposition, and
  prices them with Delivery Charges.
- **Dispatch** (WMS): re-evaluates the same rulebook with full consignment
  knowledge, filters to the proposition the customer bought, and picks the
  winner by cheapest Delivery Cost (a static order is only the final
  tie-break; missing cost data is flagged loudly, not silently skipped).

The rulebook is versioned per ADR 0003 (draft/test/publish).

Checkout handles unknown facts optimistically: constraints referencing facts
absent at basket time (dimensions, value, parcel detail) do not fire, so the
customer sees the widest honest offer. Dispatch re-evaluates with full
knowledge, and promise-then-refuse divergence is logged as a first-class
metric - the signal for moving a fact earlier (e.g. the sales platform
starting to send real dimensions). Legacy checkout requests send unknown
numerics as sentinel zeros; the Legacy Interface translates those to absent,
never to the number 0.

## Consequences

- A checkout promise that dispatch cannot honour becomes a detectable,
  reportable bug class instead of a structural blind spot; both moments
  record evaluation traces against the same rule version.
- Delivery options, allocation rules, charges, costs, and propositions
  become facets of one model, not four systems; there is nothing to keep in
  sync manually.
- Checkout evaluates on partial knowledge by design; how unknown facts are
  treated at checkout is an explicit design point of the rulebook, not an
  accident of which subsystem was asked.
- The old system's split (options vs allocation vs charges vs costs) is not
  ported; its data is migrated into the unified model.
