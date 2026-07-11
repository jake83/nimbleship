# NimbleShip Roadmap

The staged plan for building NimbleShip, the successor to the 3PL proxy.
Drafted 2026-07-11 from the founding grilling session. Each phase is roughly
an epic; deep design questions are deliberately deferred to scheduled
grilling sessions (marked **[G]**) held just before the work that needs them,
so plans reflect reality rather than guesses made months earlier.

## Decisions already locked (see docs/adr/)

1. Single tenant per instance; a second company means a second deploy
   (ADR 0001). No company-specific facts in code, ever.
2. One domain core, two protocol edges: JSON/REST is the default API; the WMS
   SOAP dialect is a logic-free legacy edge (ADR 0002).
3. Rule configurations are versioned with draft/test/publish; AI and humans
   author drafts through identical rails (ADR 0003).
4. PostgreSQL; background work on a Postgres-backed job queue, no broker
   (ADR 0004).
5. Carrier integrations are declarative specs run by one engine, with
   developer-written plugins as the bounded escape hatch (ADR 0005).
6. Stack: FastAPI + SQLAlchemy + uv, React + TypeScript + Vite, monorepo
   (/api, /web, /infra), K3s deployment (k3d locally). TDD (red/green/
   refactor) throughout. AI built directly on the Anthropic SDK, porting the
   3pl-ai-assistant orchestrator patterns. AI is additive: the dispatch path
   never depends on AI availability.

## Phase 0 - Foundations

Goal: a contributor (human or agent) can clone, test, run, and deploy locally
in minutes.

- Monorepo scaffold: /api (FastAPI, uv, pytest, ruff, mypy strict),
  /web (React/TS/Vite, vitest), /infra (Helm charts, k3d bootstrap).
- CI from the first commit: lint, typecheck, tests on every PR; container
  builds. A red suite blocks a merge (the old system learned this the hard
  way).
- Local K3s via k3d: web + worker + Postgres running from one command.
- Living docs: CONTEXT.md and ADRs continue to grow per decision.

## Phase 1 - Walking skeleton (v0.1: order in, label out)

Goal: the thinnest honest vertical slice, no AI.

- Domain core: Consignment, Parcel, Carrier, Warehouse, the append-only order
  event timeline, and the private label store (30-day prune).
- Data layer designed against the old system's failure mode: lean indexed
  event timeline as the spine; bulky payloads stored separately with
  retention; ephemeral staging data deleted, not archived.
- JSON API: request allocation, create consignment, fetch paperwork.
- Rules engine v1: versioned rule sets (draft/test/publish from day one),
  minimal rule vocabulary (allowed country, weight band), evaluation trace
  recorded for every decision.
- Drop Out carrier as the first "integration": self-generated label PDFs with
  parcel barcodes, exercising the paperwork pipeline end to end.
- Deployed on local k3d; demo: POST a consignment, get a label PDF back, see
  the timeline.

## [G] Session A - Allocation and delivery options, unified

The big domain design session, held before Phase 2. Scope: merge the four
places that currently encode "how can this ship" (allocation rules, delivery
options, delivery charges, delivery costs) into one coherent model.
Known inputs going in:

- Cheapest Delivery Cost picks the winner among suitable carriers; a static
  order is only the final tie-break; missing cost data is flagged loudly.
- Service Groups are mostly "delivery proposition" (next day, pre-10,
  economy) and need a clean re-model; their country side is murky and must be
  unpicked.
- Shipping areas / postcode surcharges stay as needs; the mechanism is
  redesigned.
- Magento's delivery-options call and the WMS allocation call must draw from
  the same model (the Metapack property the old system lost).

## Phase 2 - Allocation, unified

Goal: the routing brain, done properly.

- Implement the Session A model: delivery options, costs, charges,
  propositions, geography - one linked model, versioned per ADR 0003.
- Rules editing UI on drafts, with dry-run testing against historical orders
  and diffable version history (replaces seeder backups and update logs).
- Every allocation records a structured evaluation trace - the single source
  that will later power both the "why did this ship with X" page and the AI
  assistant. This kills the page nobody understands by fixing its data first.
- Warehouses (logical dispatch identities), collection days, holidays.
- Force allocation as a testing_tools capability: server-enforced 403 in
  production, visible banner where enabled.

## [G] Session B - Carrier definition spec language

Held before Phase 3, informed by a structured audit of all 11 existing
integrations. Scope: the declarative schema (auth, endpoints, mappings, label
extraction, error taxonomy, capability flags), the plugin interface, and
which existing carriers prove which spec features.

## Phase 3 - Integration engine and real carriers

Goal: the spec language demonstrably expresses reality.

- Integration engine v1 executing carrier definitions; definitions are
  versioned data on the draft/test/publish rails.
- Hand-write definitions for 2-3 real carriers in increasing difficulty
  (e.g. Fagans/DPD shape first, then FedEx to force the OAuth plugin).
- Plugins as needed: OAuth token provider, signing schemes; later the Dachser
  outliers (EDI emitter, DigiDocs, SSCC ranges with exhaustion refresh).
- Manifest as a first-class concept (the trigger is the WMS dispatch
  confirmation; the format is per-carrier), with retries via the job queue.
- Carrier operations as toggleable capabilities (manual manifest resend,
  availability calendar upload) - generic tools, no one-off pages.
- Tracking Events: generic store and ingestion, Voila webhook as the first
  source adapter, direct-carrier sources later.

## Phase 4 - Legacy edge and shadow mode

Goal: drop-in credibility.

- Record real WMS and Magento traffic from the live 3PL system; build the
  SOAP dialect edge as a pure translation skin, contract-tested against the
  recordings (parcels strings, barcodes, base64 labels).
- Shadow mode: replay recorded production traffic through NimbleShip and diff
  its allocations, labels, and paperwork against what 3PL actually did. This
  is the evidence pack for the workplace pitch.

## Phase 5 - The AI layer

Goal: the differentiators, each riding rails built earlier.

- 5a AI assistant: port the 3pl-ai-assistant orchestrator onto NimbleShip's
  own data. Far stronger here than on 3PL: the event timeline and evaluation
  traces are first-class, so "why did order X ship with Y / not ship at all /
  miss its manifest" is a structured read, not log archaeology. Linked from
  the dashboard, not embedded in it.
- 5b AI rules builder: conversational Q&A produces draft rule versions;
  the user dry-runs them against historical orders and publishes. **[G]**
  short session on the Q&A UX.
- 5c AI integration builder: carrier docs in, question-and-answer flow,
  draft carrier definition out; unanswerable questions become a bounded
  defer-to-developer handoff (usually "this needs a plugin"). **[G]**
  Session C on the builder UX and the developer handoff workflow.

## Phase 6 - Frontend completeness and the problem pages

Goal: everything the warehouse uses daily, at the old system's visual
standard or better (the 3PL design language is liked; keep it, use more of
the screen).

- Port the remaining admin surfaces (carrier config, customs/IOSS, blocked
  HS codes, commercial invoices, shipping areas editor).
- **[G]** Session D: Create Consignment redesign.
- **[G]** Session E: the allocation explanation page, rebuilt on evaluation
  traces so warehouse staff actually understand it.
- Dashboard: keep, enrich from the new data (manifest status, failure
  queues), AI assistant link front and centre.

## Phase 7 - Adoption path

Goal: from hobby deploy to workplace replacement.

- Production hardening: auth story, secrets, backups, observability,
  runbooks.
- Cutover plan built on shadow-mode evidence; staged carrier-by-carrier
  migration is plausible because both systems speak the same dialect to the
  WMS.
- Way-later items parked here deliberately: Teams access to the assistant,
  additional sales platforms, moving carriers off Voila (explicitly out of
  scope for NimbleShip itself).

## Working method

- One phase in flight at a time, but within a phase, chunks are sized for
  parallel agents in isolated worktrees (Herdr + treehouse): after Phase 1
  the engine, UI, tracking, and legacy edge streams rarely touch the same
  files.
- Every chunk lands through TDD (red/green/refactor) and a green CI gate.
- Grilling sessions happen just-in-time, and their outputs are ADRs,
  CONTEXT.md terms, and a PRD for the phase they unblock.

## Explicitly dropped from the old system

- ArrowXL and MetaPack-as-carrier (already dying), Norsk retry order
  (superseded by Create Consignment), branded warehouses (dead code; the
  need, per-warehouse carrier accounts, is covered by modelling carriers
  with multiple accounts), allocation seeder/backup tooling and the order
  log archive (both subsumed by better design), haulier priority as a
  primary mechanism (demoted to tie-break).
