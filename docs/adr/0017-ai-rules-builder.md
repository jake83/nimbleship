# 17. AI rules builder: granular edits on a working copy, dry-run inline, saved as a draft

Date: 2026-07-19

## Status

Accepted

## Context

Phase 5b: an operator describes a routing-rule change in conversation ("add a
Saturday FedEx service for GB over 20kg in these areas") and the AI produces a
draft rulebook version they dry-run and publish. ADR 0003 already fixes the frame:
the AI is "just another author" - it produces drafts on the identical
draft/test/publish rails as a human, never publishing directly. ADR 0008 fixes
another: AI-authored changes are reviewable one statement at a time. This ADR
records the builder's design, grilled 2026-07-18.

Rulebook versions are immutable rows; a draft is created complete (seeded from the
live version), dry-run tested against real order history, then published. Dry-run
replays the live `Consignment` table through a candidate rulebook and reports which
orders reroute. The 5a assistant (ADR 0016) established the tool-use loop, the
`LlmClient` seam, and the fail-closed config - all reused here.

## Decision

- **Granular edits on a working copy, not a whole-draft emit.** The AI starts from
  the live rulebook and calls `add_service` / `update_service` / `remove_service`
  against an in-memory working copy; untouched services are preserved byte-for-byte
  (the model never re-types them, so it can't silently perturb an unrelated
  carrier's weight band). Each edit is a discrete, named, reviewable operation - the
  unit ADR 0008 asks for - and complex routing rules build up over several turns,
  which a single-shot emit can't express reliably.
- **Dry-run inline, on the working copy, before any draft is saved.** The candidate
  rulebook is replayed over historical orders without persisting a draft, so impact
  ("reroutes 12 orders, 3 to a pricier carrier") is visible as the rule takes shape.
  Dry-run is a tool the AI can call, so it reasons about impact and self-corrects
  before the operator commits. It reuses the existing allocate-over-history engine.
- **Scope: eligibility plus the flat cost.** The builder authors the routing
  decision - every constraint field (weight bands, countries, areas, propositions,
  service groups, dimensions, girth) - plus the flat `cost` a new service needs to
  compete in the cheapest-eligible tiebreak. Banded `cost_bands`/`charge_bands` are
  out by nature, not merely deferred: they are pricing, feeding only the cheapest
  tiebreak (charges not even that), managed as a rate card elsewhere - not a routing
  rule the builder authors.
- **Never publishes; hands off to the existing rails.** On save, the working copy
  becomes a validated draft through the existing `create_draft` path (which checks
  unique codes, tie-breaks, and catalogue references). The operator reviews the
  diff, dry-runs, and publishes on the existing version page. The AI's write is
  safe by construction: a draft affects no live order, and publish stays a human
  gate.
- **Dedicated split-view builder surface.** A page with the conversation beside the
  live working copy and its inline dry-run impact - not a chat bolted into the
  field-by-field draft editor (a different interaction) and not embedded in the
  dashboard. The manual editor stays for hand-authoring; both are front doors to the
  same draft/review/publish rail.
- **Ephemeral conversation, durable rationale.** The conversation is stateless like
  5a - nothing stored, every turn recomputed against the live rulebook, so a stale
  thread never masquerades as current. But the AI's one-line rationale is captured
  as a `description` on the saved version, so the version history reads "Added
  Saturday FedEx for GB over 20kg (ops request)" rather than a diff to
  reverse-engineer - provenance operations staff can actually read.

## Consequences

- Reuses the 5a `LlmClient`/loop/config wholesale; the tools differ - they mutate a
  working copy rather than only read - but the loop shape is the same. The working
  copy is client-held and resent each turn (stateless server, like 5a's
  conversation), so the edits accumulate without server session state.
- `RulebookVersion.description` lives in the existing `data` JSON blob (version
  content), so it needs no migration; the read paths and API surface it, and a
  hand-authored draft can carry one too.
- Deliberately deferred (grill just-in-time): the banded cost/charge editor; a
  conversation audit trail beyond the draft's rationale; the Teams/Slack surface;
  and the eval suite (gated, as for 5a, before unsupervised use).
