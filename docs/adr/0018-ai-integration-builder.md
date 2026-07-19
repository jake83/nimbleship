# 18. AI integration builder: onboarding packet in, draft carrier definition out

Date: 2026-07-19

## Status

Accepted

## Context

ADR 0005 made carrier integrations declarative data run by one engine; ADR 0009
fixed what that data can say (closed transform/transport/label vocabularies,
multi-step operations, named plugins at four extension points) and noted the
payoff: definitions "render as forms in a UI, and are safe targets for the AI
onboarding flow, which fills in rows - not code - from carrier docs plus Q&A."
ADR 0016 (assistant) and ADR 0017 (rules builder) then built the in-process
tool-use loop, the `LlmClient` seam, and the pattern of granular edits to an
in-memory working copy handed off to the ADR 0003 draft/test/publish rails.

Phase 5c is the third AI feature: turn a carrier's onboarding material into a
draft `CarrierDefinition`. Session C decided the builder UX and the
developer-handoff workflow. The grounding reality, from the owner: carriers
arrive as a messy, varied bundle - an email with doc attachments, sometimes
pasted text, sometimes endpoint URLs with usernames and passwords - forwarded by
**non-technical operators** who "just forward it and ask, can you do this?" The
technical work is done by the owner today; the flow must make the AI (and a
bounded engineer escalation) do it instead.

## Decision

- **Reuse the 5b builder wholesale.** Same tool-use loop, `LlmClient` seam, and
  the working-copy-plus-granular-edits shape (ADR 0017), operating on a draft
  `CarrierDefinition` instead of a rulebook, handed off to ADR 0009's existing
  golden-replay test and publish gate. The genuinely new surface is only two
  things: ingesting the packet, and the engineer handoff. 5c is "5b's pattern +
  ingestion + handoff", not a new engine.

- **Input is an onboarding packet, not a rigid form.** The operator can, in any
  combination, upload files (doc attachments), paste raw text (a forwarded
  email), and enter endpoint URLs with credentials. The AI reads whatever is
  provided. The interface offers the three affordances (attach / paste /
  endpoints) rather than demanding one document.

- **Two personas, one draft.** The **operator** is non-technical: they dump the
  packet and answer only plain-language gaps ("is this the live or test
  endpoint?"). The **engineer** is a first-class second persona who owns every
  technical escalation on the same draft. "Defer to engineer" is therefore the
  single escalation seam for *any* technical blocker - a missing plugin or a
  question the docs do not answer - not merely a failure fallback.

- **Secrets never reach the model; the AI never calls the carrier.** Credentials
  the operator pastes route straight into the Carrier Config store (ADR 0009's
  `config.*`), and the AI is told only that a secret exists at `config.apiKey`,
  never its value. The AI is purely doc-grounded: it drafts from the packet but
  makes no outbound requests. Any real call is a separate, human-triggered action
  the *engine* performs with the stored config - which also records the first
  golden request/response for a brand-new carrier (ADR 0009's Tier-2-seeds-Tier-1
  path), rather than trusting an LLM to fire authenticated traffic with
  real-world side effects.

- **The handoff is a first-class blocker on the draft.** When the AI hits a
  wall it creates a structured blocker: its type, what is needed, the doc excerpt
  that triggered it, and what it already tried - auditable on the ADR 0003 rails,
  not an out-of-band email. A blocker parks only its part: the AI keeps building
  everything else it can, so the engineer inherits a mostly-complete draft with a
  sharp, isolated gap.
  - **Needs a plugin** resolves through code, and the draft enforces it: the AI
    writes a reference to a named-but-unimplemented plugin plus the spec of what
    it must do. ADR 0009's *authoring* validation (the publish gate, not the more
    lenient load-time read) refuses a definition naming an unregistered plugin, so
    the publish gate *is* the handoff gate. This holds today only for computed-field
    plugins, which are validated against the registry at authoring; the other
    extension points, notably auth plugins, tolerate an unregistered name (a draft
    naming one would publish and fail only at booking). Closing that - the same
    authoring-time unknown-plugin rejection at every extension point - is a 5c
    prerequisite, small and squarely in ADR 0009's model (it always intended
    plugins validated at authoring; the check simply was not built where no carrier
    yet needed it). The engineer then implements the plugin as a normal reviewed PR;
    once deployed, the reference resolves.
  - **Needs a decision** resolves through an answer the engineer records on the
    blocker (a value, a config key, "use the v2 endpoint"), which the AI consumes.
  - The draft carries a visible **blocked-on-engineer** state and cannot publish
    while any blocker is open. When a plugin ships or a decision lands, the flow
    **auto-resumes** the AI on that blocker and notifies the operator - keeping
    the non-technical operator out of the loop until there is something to see.
  - The engineer works in a **separate technical surface** on the same draft (raw
    definition, blockers, the packet), not inside the operator's chat thread.

- **The operator sees a capability status board, doc-derived on an engine-bounded
  frame.** The rows are the operations the engine can actually perform with a
  carrier (book a shipment, produce a label/paperwork, send a manifest, ...) -
  bounded and engine-owned, the same principle as ADR 0009's vocabularies,
  because a status for something the engine cannot invoke is meaningless. But the
  AI *populates and prunes* the board from the packet per carrier: applicability
  (a carrier with no manifest shows `Manifest · N/A`, not blocked; a label-on-book
  carrier folds label into booking), structure (PalletForce's "book" is really
  book-then-fetch-label; Dachser mints SSCCs), and state (`drafted from docs`,
  `needs engineer`, `needs a plain answer`, `N/A`) - all inferred and all
  overridable by the operator or engineer. The board never shows a mapping entry;
  the raw definition lives in the engineer's technical surface. The board doubles
  as the readiness signal: publishable when every *applicable* row is drafted with
  no open blockers, which ties straight to ADR 0009's publish gate.

- **Testing reuses ADR 0009 unchanged.** Golden replay plus config completeness
  is the publish gate. A brand-new carrier with no golden corpus bootstraps from
  the human-triggered engine call above, and the AI may additionally propose the
  doc's own example payloads as offline golden fixtures. No new test model.

## Consequences

- The declarative vocabulary is the AI/engineer boundary, mechanically enforced:
  anything expressible in ADR 0009's rows is the AI's to draft, anything needing
  a plugin defers, and the publish gate (unknown-plugin rejection, config
  completeness, golden replay) refuses to let a half-resolved draft go live. No
  new *kind* of gate is invented - only the existing authoring-time unknown-plugin
  rejection has to be extended from computed-field plugins to every extension
  point (the prerequisite above).
- Non-technical operators are first-class, not tolerated: they never see
  definition guts or secrets, and the capability board answers their only real
  question - "how far along, and what is it waiting on?" The engineer never has
  to scroll a conversation.
- The handoff being on-rails (a draft blocker, not an email) means an onboarding
  in progress is visible, resumable, and auditable - and asynchronous by nature,
  since a plugin is a PR and a deploy.
- Deliberately deferred (grill just-in-time): the PDF-extraction pipeline (start
  with pasted text and text attachments); the exact plain-language Q&A prompts;
  and how the board treats tracking (Voila is a carrier-agnostic aggregator
  today, ADR 0014, not a per-carrier definition operation). The eval suite is
  owed before unsupervised use, as for 5a/5b.
- Proving customers are already chosen: ADR 0009 left DPD and PalletTrack as the
  first real customers of this flow.
