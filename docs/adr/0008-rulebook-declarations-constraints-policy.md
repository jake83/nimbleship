# 8. Rulebook structure: declarations, constraints, policy

Date: 2026-07-11

## Status

Accepted

## Context

The old allocation engine models everything as a hierarchy of AND/OR rule
groups with TRUE/FALSE/NULL return values cascading into a global verdict, in
haulier-scoped trees evaluated in an authored order. It is expressive but
unreadable: no warehouse user (and few developers) can predict or explain an
outcome, which is why the routing-explanation page failed. The 35-option
vocabulary also mixes three kinds of fact: shipment properties, carrier
service properties, and order-origin properties wearing hack costumes
(ID-prefix and email-domain inference), plus carrier-specific code leaking in
as pseudo-rules.

NimbleShip needs rules a warehouse user can author (with AI help), review,
test, and understand - and every decision must be explainable (ADR 0007).

## Decision

The eligibility rulebook has three layers:

1. **Service declarations** - facts on the carrier service, not authored
   rules: weight range, dimensions, countries with lead times, areas
   served/blocked, capabilities (two-man, pallet, Saturday), and which
   Delivery Propositions the service fulfils. Shipments match declarations
   automatically.
2. **Constraints** - the authored rulebook: flat, individually named
   statements with a scope (which services/carriers), a condition over
   shipment and origin facts (AND/OR allowed within a condition), and a
   single effect: block. All matching constraints apply; there is no
   ordering, no precedence, and no cross-rule state.
3. **Selection policy** - fixed, not authored: among eligible services in
   the bought proposition, cheapest Delivery Cost wins; a static carrier
   order is only the final tie-break; missing cost data is flagged, never
   silently skipped.

Carrier-specific dynamic checks (Furdeco's next-day calendar, PalletForce's
postcode-service lookup) are not constraints: they are capability checks
declared by the carrier definition (ADR 0005), evaluated live at dispatch
time only.

Order-origin facts are stated explicitly by callers ({platform, website,
marketplace}); the Legacy Interface derives them from old signals (order ID
prefix/length, recipient email domain, custom fields) via per-caller
translation config, so inference hacks never enter the core.

## Consequences

- Every exclusion has a human-readable name; an allocation trace reads
  "matched declarations; excluded by [constraint names]; cheapest of the
  rest" - the routing-explanation page becomes a rendering exercise.
- AI-authored changes are reviewable one statement at a time, on the
  draft/test/publish rails of ADR 0003.
- Unordered all-blocks-apply semantics means behaviour is insensitive to
  rule position - simpler to test, no precedence bugs.
- Expressiveness is deliberately reduced versus nested groups; anything that
  genuinely cannot be said as declarations + named blocks must become a
  capability check or a selection-policy change, both explicit code-level
  decisions.
- Migration decomposes the 42 live rule groups into declarations (most
  weight/country/area content) and a short named constraint list (the
  origin/marketplace blocks); the exercise doubles as validation that the
  model expresses current behaviour.
