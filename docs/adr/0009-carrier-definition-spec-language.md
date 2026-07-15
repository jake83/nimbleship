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

A definition is validated at two distinct moments. **Authoring** - draft
creation, and the publish gate (`definition_for` → `model_validate`) - runs
every rule: nothing invalid ever enters the store. **Load** - the booking and
manifest-send runtime read (`active_definition` → `CarrierDefinition.load`) -
validates a stored, already-published definition, and skips the rules whose
only failure mode on a stored violator is a *clean booking failure*.

The split exists because re-validating a stored row against a *tightened* rule
would retroactively reject a definition that was legitimate when published,
breaking booking for a live carrier the moment the rule moved - even though the
engine can still render that definition. So load asks only "can the engine run
this safely?", not "would this pass authoring today?".

The line between skippable and strict is drawn by the *failure mode*, informed
by the booking flow's error handling (a render `ValueError` is routed to a
`booking_failed` 502, and its commit is guarded to a 409 under a duplicate
race - both clean):

- **Skippable on load** - authoring-policy rules whose stored violator fails
  *cleanly* at render: the csv scalar-column rule (the renderer refuses a list
  column), and source/target resolution (an unresolvable fact raises at render,
  now caught). Re-enforcing these on load buys nothing but stranding.
- **Strict on load** - two classes. Rules whose violation is an *unsafe side
  effect*, not a clean failure: an SSCC that does not halt would mint a
  wrapping range and reissue live codes; a fan-out on a non-upload transport
  would double-submit on retry; an allocate minting from a non-config prefix.
  And every *structural* rule (one value origin per entry, legal xml targets,
  an upload step with a filename) - shapes the engine cannot render around.
  For all of these the clean load-time rejection IS the safe outcome, so
  stranding is correct: the definition is genuinely broken or dangerous.

The clean-failure routing is a prerequisite: skipping a render-affecting rule
on load is only safe because a render failure at booking now surfaces as a
`booking_failed` 502/409 rather than an uncaught 500 after minting. Before that
routing existed, none of these rules could have been skipped.

Definition files may carry top-level commentary (e.g. a `notes` array) to
explain a carrier to a human author. It is not a schema field: the model
ignores it, it never persists onto `CarrierDefinition`, and nothing at runtime
may read it. Commentary that must survive belongs in the definition's stored
provenance, not in a field the schema silently drops.
