# 9. Carrier definition spec language: declarative mappings, closed vocabularies

Date: 2026-07-12

## Status

Accepted

## Context

ADR 0005 decided carrier integrations are data executed by one engine.
Session B, informed by an audit of all 11 existing integrations (10,625
lines of hand-written code), had to decide what that data can SAY - above
all, how a definition expresses request-building.

Alternatives considered:

- A full template language (Jinja-style) embedding expressions in the
  payload: maximally expressive, but templates are code wearing a data
  costume - unvalidatable at draft time, unrenderable in a UI, unsafe for
  AI generation, and the plugin boundary evaporates.
- A sandboxed expression language (CEL) per field: safer, but unreadable to
  warehouse users and weakly validatable.
- Declarative field mappings with a closed transform vocabulary.

The audit found ~8 of 11 carriers reduce to auth + templated request +
response extraction, with the genuinely exotic behaviour converging on ten
plugin candidates in four clusters: auth schemes, pre-booking checks,
computed fields, and post-booking transforms.

## Decision

A carrier definition is a versioned document (ADR 0003 rails) whose
request-building is a list of mapping entries: target field, source fact
(from the same named-fact vocabulary the rulebook uses: shipment, warehouse,
config, prior step outputs), and optionally transforms from a closed,
engine-owned vocabulary (join, uppercase, lookup-table, unit conversions,
format-date, split...). Response handling is declared extraction: paths,
success conditions, error message locations.

Structural affordances the schema includes natively:

- **Multi-step operations**: an operation is a sequence of requests; each
  step's extracted outputs are facts available to later steps' mappings
  (PalletForce's book-then-fetch-label).
- **Transport vocabulary**: http, ftp_upload, sftp_upload, local_render -
  small engine-owned transports, credentialed from per-install config.
- **Label source vocabulary**: base64_pdf, png_pages, jpg, fetch_step,
  local_render, with post-process plugins for stitching/overlay cases.
- **Named plugins** at four extension points: auth schemes, pre-booking
  checks, computed fields, post-booking transforms. Needing a missing
  plugin IS the defer-to-developer path (ADR 0005).

The dividing rule extends ADR 0008: the engine's vocabularies are the only
code; every carrier-specific fact - and the arrangement of mappings - is
data. When a carrier wants something the vocabulary cannot say, the answer
is a new engine transform (reviewed PR) or a named plugin (bounded code),
never a clever expression inside a definition.

MetaPack is deliberately excluded: it is the dying aggregator whose dialect
lives at the legacy edge (ADR 0002), not a carrier definition.

## Consequences

- Definitions validate at draft time (unknown facts, malformed transforms
  fail authoring), render as forms in a UI, and are safe targets for the
  AI onboarding flow, which fills in rows - not code - from carrier docs
  plus Q&A.
- The acceptance test for the schema is expressing the existing carriers:
  the audit sizes Furdeco at ~50 lines of definition plus one plugin,
  against 1,203 lines of legacy code.
- The transform vocabulary grows deliberately by engine PR; expressiveness
  pressure surfaces as vocabulary/plugin requests, never as unreviewable
  cleverness in data.
- Multi-step state and transports live in the engine, keeping stateful
  orchestration (the PalletForce dance) declaratively visible but
  mechanically owned by tested code.

## Testing model and proving ladder (Session B, same day)

A draft definition's "test" step (ADR 0003) means **golden replay, plus
optional sandbox**:

- Tier 1, required for publish, fully offline: render the draft's requests
  against historical shipments and diff against recorded golden
  requests/responses. Proves mapping fidelity with zero carrier contact and
  runs in CI. The engine records every real request/response to build the
  golden corpus.
- Tier 2, optional and explicit: live calls against a carrier's
  sandbox/test endpoint where one exists, results attached to the draft.

The Phase 3 proving ladder, each rung proving exactly one new capability:
DropOut (local_render + engine core) -> Furdeco (single-call REST, query
auth, XML extraction, first computed-field plugin) -> FedEx (OAuth plugin,
PNG label pages, customs commodities) -> PalletForce (multi-step
operations, number-range plugin). Fagans proves the ftp_upload transport
when convenient; Dachser (SSCC + SFTP EDI + DigiDocs) is its own mini-epic
at the end; DPD and PalletTrack are deliberately left as the first real
customers of the Phase 5 AI onboarding flow.

## Authoring validation vs load validation (amended 2026-07-15)

A definition is validated at two distinct moments, and they are not the same
gate. **Authoring** - drafting or publishing - runs every rule via
`CarrierDefinition.model_validate`: unknown facts, malformed transforms, and
authoring-policy rules (a csv column must be scalar, a fan-out must be a
manifest on an upload transport, an allocate must sit on the book operation)
all fail here, before the row is ever stored. **Load** - reading a stored,
already-published definition at booking time - runs through
`CarrierDefinition.load`, which validates structure only and skips the
authoring-policy rules.

The split exists because the two moments answer different questions. Authoring
asks "should this be allowed into the system?" and enforces current policy in
full. Load asks "can the engine render this?" - and a definition that was
valid when published can always be rendered, whatever an authoring-policy rule
later tightens to. Without the split, tightening a policy rule would
retroactively strand live definitions that were legitimate when stored, so a
policy change could silently break booking for an in-flight carrier.

A rule is authoring-policy (skippable on load) only when a stored violator
still loads to something safe - a shape or placement rule the engine tolerates
or independently guards at render. Two classes stay strict even on load,
because for them the clean load-time rejection *is* the safe outcome and
skipping would be worse than stranding: rules whose violation the engine cannot
render around (an unresolvable source names a fact that does not exist, so
skipping it defers a clean failure into an uncaught render error mid-booking,
after SSCC minting has committed), and rules whose violation causes an unsafe
side effect (an SSCC allocation that does not halt would let a fresh sequence
mint a wrapping range and reissue live codes - the sequence-row policy lock
guards only ranges that already exist). Ordinary structural rules (exactly one
value origin per entry, legal xml targets, an upload step with a filename) are
strict on load for the same reason: they are shapes a violation makes a bug
wherever it surfaces, not a policy that moved under a stored row's feet.

Definition files may carry top-level commentary (e.g. a `notes` array) to
explain a carrier to a human author. It is not a schema field: the model
ignores it, it never persists onto `CarrierDefinition`, and nothing at runtime
may read it. Commentary that must survive belongs in the definition's stored
provenance, not in a field the schema silently drops.
