# 12. Service Group: a distinct eligibility axis, not a Delivery Proposition

Date: 2026-07-16

## Status

Accepted

## Context

The WMS filters allocation by **service groups**. A service group is a
hand-curated allow-list of carrier services - the MetaPack "Setup Service
Groups" screen adds specific carrier services into a named group (e.g.
`AFTERSALE` holds DPD 24 Hour, DPD 48 Hour, DX 1-Man Overnight, DX Overnight
Pallet, DX 3-Day, Evri 48 Hour). It mixes carriers and speeds; it is emphatically
not a single customer-facing promise. On each order the WMS sends a requested
group (`custom1`) and, at allocate, a set of accepted groups
(`acceptableCarrierServiceGroupCodes`); dispatch may use only services that are
members of an accepted group. The WMS depends on this mechanism, so NimbleShip
must carry it through.

CONTEXT.md originally framed **Delivery Proposition** as replacing "the promise
half of the old service groups," implying a service group was a promise that
would dissolve into a proposition plus order-type facts. The audit shows that is
not what a service group is: it is a carrier-service eligibility set, not a
promise. So it cannot fold cleanly into the checkout proposition.

Two ways to model it:

- **A - generalise Delivery Proposition.** Treat each service group as a
  proposition, make the proposition filter set-valued, share one catalogue.
  Minimal new machinery, but it re-overloads "service group" - the exact term
  Delivery Proposition was created to escape - and mixes checkout promises
  (`next-day`) with carrier-eligibility groupings (`FEDEX`, `AFTERSALE`) in one
  concept.
- **B - a distinct Service Group axis.** Keep Delivery Proposition as the clean
  checkout promise a JSON caller states; add Service Group as a separate
  legacy-originated eligibility concept.

## Decision

Model Service Group as its own eligibility axis (Option B), riding the existing
declaration-check rails (ADR 0008 addendum: a new declaration kind is one check
+ one declaration field + tests):

- A `ServiceGroup` catalogue (company data, API-managed, seeded demo, referenced
  by immutable rulebook versions; create/update, no delete) - the same shape as
  the Delivery Proposition catalogue.
- `ServiceDeclaration.service_groups`: the groups a service is a member of,
  authored in the rulebook and validated against the catalogue - the declarative
  replacement for MetaPack's manual "services in the group" list.
- `Shipment.accepted_service_groups`: the accepted set as a fact.
- `ServiceGroupCheck` in `ALL_CHECKS`, with **allow-list** semantics that
  deliberately differ from `PropositionCheck`:
  - An empty accepted set does not restrict (optimistic, ADR 0007) - so the JSON
    path, which never sends groups, is unaffected.
  - A non-empty accepted set makes a service eligible only if its memberships
    intersect the accepted set. A service that declares no group is unreachable
    under a filter - **not** a wildcard (the opposite of an empty proposition
    declaration).

The Legacy Interface validates its own dialect input and maps to its own error
shape, keeping that policy out of the caller-agnostic domain (the edge-owns-its-
error-shape split of ADR 0002, the same shape as the per-caller translation
config of ADR 0008):

- The accepted set is `custom1` (the requested group) unioned with
  `acceptableCarrierServiceGroupCodes` (the accepted set).
- WMS codes are adopted verbatim as the catalogue codes - no remap.
- A legacy order that carries no group at all faults, matching the WMS's own
  "no service group provided -> no services returned" behaviour (surfaced as a
  paperwork fault). The domain's optimistic-empty rule is not used for the
  legacy path because the edge has already required a group.
- An accepted code absent from the catalogue faults (blanket) - it means the WMS
  knows a group the catalogue does not, a sync gap to fix loudly. Softening it
  (drop unknown, filter by the rest) risks an all-unknown set collapsing to
  empty -> optimistic -> unrestricted, allocating to services the customer never
  accepted.

## Consequences

- Two structurally similar membership filters (Delivery Proposition and Service
  Group) coexist by design. That duplication is the accepted cost of keeping two
  distinct meanings apart - a checkout promise the customer bought, versus a
  legacy carrier-service allow-list the WMS imposes - rather than re-overloading
  one term.
- The JSON/checkout path is untouched: it filters by proposition and never
  populates `accepted_service_groups`.
- Onboarding a service for legacy dispatch now means declaring its group
  memberships in the rulebook - the declarative equivalent of MetaPack's "add
  service to group" step.
- Rollout: this makes the group filter mandatory for legacy orders, so every
  legacy-dispatchable service must declare its memberships. A rulebook version
  published before this axis existed (including a pre-0012 demo seed) has none,
  so a legacy order against it correctly finds no eligible service - inherent to
  introducing an allow-list, not a bug to code around. The demo seed is updated
  for fresh installs; a real cutover republishes rulebooks with memberships
  (and re-seeds a stale dev catalogue) before the WMS relies on group filtering.
- Order Origin and order-type facts (MARKETPLACE, AFTERSALE as order-type)
  remain deferred: no constraint check or Customs Identity consumes them yet, so
  deriving them now would build facts nothing reads.
