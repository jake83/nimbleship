# 16. AI assistant: an in-process, read-only tool-use loop over structured domain reads

Date: 2026-07-18

## Status

Accepted

## Context

Phase 5a is the AI assistant: the operator asks "why did order 123456 ship with
Furdeco when it should have shipped with Dachser?" or "why did order X fail to
print labels?" and gets a grounded answer, instead of doing log archaeology.

NimbleShip is far better placed for this than the incumbent was. The reference
`3pl-ai-assistant` is a *separate* service bolted onto a system it does not own -
it reaches into the 3PL proxy's database over the wire and parses log files to
reconstruct what happened. NimbleShip owns clean, structured data: an append-only
order event timeline (`order_events`, ~11 stages), and allocation evaluation traces
persisted as `AllocationResult` JSON on `Consignment.allocation` - each rejected
service carries the exact named `Check` that failed with its `expected`/`actual`
(ADR 0007/0008). So the flagship questions are a *structured read*, not a guess:
the reason a carrier was or wasn't chosen is already in the trace.

There is no AI scaffolding yet; this is greenfield and phase-establishing.

## Decision

- **In-process, read-only module** (`nimbleship.assistant`), not a separate service.
  The reference assistant was separate only because it was external to its data;
  NimbleShip's module reads its own domain directly and inherits the same
  test/type/lint rails. What is ported is the *orchestrator pattern* - a Claude
  tool-use loop (system prompt + read-only tool registry + context truncation) with
  the `anthropic` SDK, no agent framework - not the topology or the log-parsing.
- **Surface-agnostic core:** the module is a function of `(order_number,
  conversation) -> answer`. The v1 web chat and a future Teams/Slack webhook are
  both thin callers; the interaction model (conversational, with follow-ups) does
  not change when the surface does.
- **First slice: single-order diagnostics.** Four read-only tools, all keyed by an
  order number the operator supplies: `order_timeline`, `allocation_trace`,
  `tracking`, `manifest_status`. This answers the three flagship questions and
  needs no new schema. Cross-order/aggregate queries ("every order excluded by
  rule Y this week") and lookup-by-attribute ("the order for customer Z") are
  deferred - the former needs trace indexing that does not exist yet.
- **Grounding is prompt-first.** The tools return precise structured facts (the
  failing check with `expected`/`actual`, the cost comparison, the `label_failed`
  error detail); the system prompt requires every claim to name the check or event
  it came from and to say the data does not show it rather than speculate. Because
  the answer is deterministic and already in the trace, the model mostly *reads and
  explains* - it does not reason its way to the reason. A structured-evidence
  contract (return the trace rows behind each claim, for an auditable UI) is a
  natural later add.
- **Wiring:** `NIMBLESHIP_ANTHROPIC_API_KEY` via the existing pydantic Settings,
  **fail-closed** (unset by default; when unset the assistant reports "not
  configured" rather than erroring, like `voila_webhook_secret`). Model is
  configurable (`NIMBLESHIP_ANTHROPIC_MODEL`), default **Sonnet 4.6** - the work is
  trace-reading and explanation, for which Sonnet is fast and cheap enough for
  interactive use; Opus is available via config for hard cases, and swappability
  matters for A/B-ing a model change against the eval suite.
- **Build order:** two PRs - the module (tool-use loop + the four tools + system
  prompt), fully testable without a UI, then the FastAPI route and the chat page
  (shadcn/ui on the existing Tailwind stack), linked from the dashboard.

## Consequences

- **Evals are owed, not optional - gated on the trust threshold.** The first slice
  ships prompt-grounded with a human in the loop (the operator reads the answer
  before acting). A regression eval suite - golden order scenarios with known-correct
  explanations, diffed against the assistant's answer - is required *before* the
  assistant moves to Teams (acted on with little oversight) or is relied on by anyone
  who is not the operator. It reuses the shadow-mode record-replay-diff shape (ADR
  0015), so it is not a from-scratch effort. Tracked in ROADMAP.md Phase 5a.
- Read-only by construction: the tools only query, so the assistant is safe to run
  against live data and cannot corrupt state.
- **Conversation is ephemeral.** The route is stateless - the caller passes the full
  conversation each turn and nothing is persisted. This is deliberate beyond
  simplicity: carrier rules and rulebook versions change constantly, so a stored
  transcript ("shipped with X because Y failed the weight check") would read as
  fact weeks later when it may no longer be true. Every answer is computed against
  current data and the live trace. A persisted transcript, if ever wanted, is an
  explicit audit feature, not a default. The web surface is a homepage launcher bar
  that opens a dedicated chat page (a launcher, not the assistant embedded in the
  dashboard); a fail-closed status endpoint lets it disable input when unconfigured.
- The trace read is only as historical as `Consignment.allocation`: a re-allocation
  overwrites it (no separate historical-trace table), so "why was it decided on a
  past date, before an override" is not answerable yet. A persisted trace history is
  a later concern, surfaced if re-allocation audit is needed.
- Deliberately deferred (grill just-in-time): aggregate/cross-order queries and the
  trace indexing they need; lookup-by-attribute; the Teams/Slack surface (Phase 7);
  cost/rate controls (only if it gets hammered); the structured-evidence contract.
