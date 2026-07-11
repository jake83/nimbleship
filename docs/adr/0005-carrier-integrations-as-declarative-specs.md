# 5. Carrier integrations are declarative specs, not code

Date: 2026-07-11

## Status

Accepted

## Context

NimbleShip's headline feature is AI-assisted carrier onboarding: a user
provides the carrier's API documentation, answers questions the AI asks, and
the system gains a working integration - with a defer-to-developer path for
what the user cannot answer.

In the old 3PL system every integration is hand-written PHP (one directory
per carrier). Evidence from those 11 integrations: most follow the same
shape - authenticate, POST a consignment payload, receive labels and tracking
references - while a minority are genuinely exotic (Dachser: EDI file
generation, DigiDocs, SSCC ranges; FedEx: OAuth token flow; PalletForce:
consignment number generation).

If the AI's output were runnable code, user-triggered AI output would become
production code, requiring sandboxed execution, review gates, and redeploys
per integration - unbounded failure modes, and hard to let warehouse users
drive. If a developer must review generated code every time, the developer is
back in every onboarding, defeating the goal.

## Decision

A carrier integration is data: a versioned, declarative carrier definition
(auth method, endpoints, request/response mappings, label extraction, error
handling, capability flags) executed by a single well-tested integration
engine. The AI builder reads carrier docs and the user's answers and produces
a draft definition, which goes through the same draft/test/publish rails as
routing rules (ADR 0003).

Exotic requirements are handled by developer-written plugins (e.g. an OAuth
token provider, an EDI file emitter, a signing scheme) that a definition can
reference by name. Needing a plugin that does not exist yet IS the
defer-to-developer path: the definition captures everything else, and the gap
is a small, well-bounded piece of code.

## Consequences

- Onboarding a mainstream carrier requires no deploy, no sandbox, and no
  developer; publishing a definition is a data change with test evidence.
- The engine is the single point of correctness: it is built TDD with high
  coverage, and every definition is exercised against recorded/sandbox
  traffic before publish.
- The expressiveness ceiling is explicit: what the spec language cannot say,
  a plugin must. The spec language grows deliberately (versioned schema)
  rather than integrations accreting bespoke code.
- Existing carriers are migrated by writing definitions (plus the handful of
  plugins the outliers need), which doubles as the spec language's acceptance
  test: it must express all 11 current integrations.
