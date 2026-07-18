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
6. Stack: FastAPI + SQLAlchemy + uv, React + TypeScript + Vite with
   shadcn/ui on Tailwind (ADR 0006), monorepo (/api, /web, /infra), K3s
   deployment (k3d locally). TDD (red/green/refactor) throughout. AI built
   directly on the Anthropic SDK, porting the 3pl-ai-assistant orchestrator
   patterns. AI is additive: the dispatch path never depends on AI
   availability.

## Phase 0 - Foundations

Goal: a contributor (human or agent) can clone, test, run, and deploy locally
in minutes.

- Monorepo scaffold: /api (FastAPI, uv, pytest, ruff, mypy strict),
  /web (React/TS/Vite, vitest), /infra (Helm charts, k3d bootstrap).
- CI from the first commit: lint, typecheck, tests on every PR; container
  builds. A red suite blocks a merge (the old system learned this the hard
  way).
- AI adversarial review on every PR: two independent Claude review jobs via
  claude-code-action (one standard reviewer, one prompted purely to refute
  the change), authenticated with a Claude Max OAuth token
  (claude setup-token -> CLAUDE_CODE_OAUTH_TOKEN secret) - no API billing.
  Branch protection requires green tests + lint + typecheck + review before
  merge. A second model (e.g. OpenAI Codex GitHub integration) can be added
  later for cross-model diversity if same-model blind spots show up. The
  end state to demo: no human in the deploy path except approval.
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

## [G] Session A - Allocation and delivery options, unified (HELD 2026-07-11)

Held on 2026-07-11; outcomes are ADRs 0007 and 0008 plus CONTEXT.md terms
(Delivery Proposition, Constraint, Order Origin, Customs Identity). Summary:

- One eligibility rulebook consulted at checkout and dispatch; checkout
  projects to propositions priced by Delivery Charges, dispatch picks
  cheapest by Delivery Cost within the bought proposition (ADR 0007).
- Rulebook structure: service declarations + flat named block-constraints +
  fixed selection policy; carrier dynamic checks become capability checks on
  carrier definitions (ADR 0008).
- Order origin ({platform, website, marketplace}) is an explicit fact;
  legacy signals (ID prefix/length, email domain, custom fields, service
  group codes) are translated at the edge via per-caller config.
- Customs Identity generalises IOSS: every international order resolves to
  a deemed supplier (own registrations for direct sales, the marketplace's
  for marketplace orders).
- Checkout unknown facts handled optimistically with divergence logging;
  legacy sentinel zeros translate to absent facts.
- Deferred to Phase 2 detail: charge/cost rule shapes (port the existing
  band structures initially), shipping-area mechanism redesign, the
  haulier_postcode_surcharge table (apparently dead - confirm and drop),
  and the 40-row service-group mapping table.

## Phase 2 - Allocation, unified (DELIVERED 2026-07-12)

Delivered via foundations F1/F2 (PRs #12/#13), six parallel agent chunks
(PRs #14-#19), and the integration step: Alembic-owned schema with Postgres
in CI, the evaluator registry with seven declaration kinds, banded Delivery
Costs driving selection, warehouse-scoped Delivery Charges with the
/api/quotes checkout projection, geography (every matching prefix counts),
the Delivery Proposition catalogue, Warehouses with collection calendars,
the rules UI (versions, diff, draft editor, dry-run, publish), dry-run
replay that re-resolves area facts, and force-allocation behind the
testing_tools capability. Carried forward: delivery dates/cutoff times,
per-website warehouse mapping, the models.py naming split (Phase 3), rules
UI editing for cost/charge bands.

Original goal: the routing brain, done properly.

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

## [G] Session B - Carrier definition spec language (HELD 2026-07-12)

Held 2026-07-12 against a full audit of all 11 integrations (10,625 lines;
~8 carriers reduce to auth + templated request + extraction; ten plugin
candidates in four clusters). Outcome: ADR 0009 - declarative mapping
entries with closed transform/transport/label vocabularies, multi-step
operations, named plugins at four extension points; golden replay +
optional sandbox as the test gate; proving ladder DropOut -> Furdeco ->
FedEx -> PalletForce, Fagans for ftp_upload, Dachser as its own mini-epic,
DPD/PalletTrack reserved as the AI onboarding flow's first real customers.
MetaPack explicitly excluded (legacy edge, not a definition).

## Phase 3 - Integration engine and real carriers (DELIVERED 2026-07-18)

Delivered: the integration engine (render + execute, golden-replay traffic), the
carrier-definition spec language with authoring-vs-load validation (ADR 0009),
the six-rung proving ladder (DropOut, Furdeco, FedEx, PalletForce, Fagans,
Dachser - each with a definition and tests), the plugin seams in use
(`oauth_client_credentials`; the number-range/SSCC and consignment-number field
plugins; the ftp/sftp uploaders), Manifests as a first-class concept with
job-queue retries and warehouse-local dates, and Tracking Events (store + Voila
webhook + read API, ADR 0014). Every carried-forward hardening item below is
resolved except stalled send-job recovery (Phase 7). Two items carry forward as
genuinely open: carrier operations as toggleable capabilities, and direct-carrier
tracking sources (blocked on a real webhook spec - the reference only ever
ingested Voila; the adapter seam is a drop-in the moment one exists). The two
plugin extension points ADR 0009 names but no carrier yet needs (pre-booking
checks, post-booking transforms) and DigiDocs stay deferred by design.

Original goal: the spec language demonstrably expresses reality.

- Integration engine v1 executing carrier definitions; definitions are
  versioned data on the draft/test/publish rails.
- Hand-write definitions up the proving ladder (ADR 0009): DropOut,
  Furdeco, FedEx, PalletForce; Fagans for the ftp_upload transport;
  Dachser as its own mini-epic.
- Plugins as needed: OAuth token provider, signing schemes; later the Dachser
  outliers (EDI emitter, DigiDocs, SSCC ranges with exhaustion refresh).
- Manifest as a first-class concept (the trigger is the WMS dispatch
  confirmation; the format is per-carrier), with retries via the job queue.
- Carrier operations as toggleable capabilities (manual manifest resend,
  availability calendar upload) - generic tools, no one-off pages. (OPEN - not
  yet built; carries forward.)
- Tracking Events: generic store and ingestion, Voila webhook as the first
  source adapter, direct-carrier sources later. (Store, Voila webhook, and read
  API delivered; a direct-carrier source is OPEN, blocked on a real webhook spec;
  the tracking UX stays deferred.)

Carried forward (manifests + job queue, PR #34):

- Publish render-gate coverage for manifest operations (resolved): the gate
  now renders a non-fan-out manifest operation once against a manifest
  synthesized from the carrier's own recent consignments (a fan-out manifest
  gates per shipment, like a book op), so a broken manifest mapping is caught
  at publish, not at trailer-close. The `_render_gate` docstring is corrected.
- Warehouse-local manifest date semantics (resolved): `Warehouse.timezone` is a
  required field and `manifest_facts` computes the date in the warehouse's local
  zone, so a near-midnight scan-out declares the day the warehouse observes. The
  related gap is closed too: `send_manifest` faults loudly when a manifest names a
  Warehouse row that no longer exists, rather than silently omitting its facts.
- Stalled send-job recovery: a worker killed mid-send leaves the job in
  Procrastinate's `doing` state forever and the Manifest `pending` with no
  alarm. Wire stalled-job requeue (heartbeat + periodic reset); production
  hardening, naturally Phase 7.

Carried forward (ftp_upload + Fagans, PR #36):

- CSV/formula injection defence (resolved): the shared CSV renderer neutralises
  a field starting with `=`/`+`/`-`/`@` (and tab/CR) by prefixing a single quote,
  applied on every field. The mitigation is universal rather than a per-carrier
  flag - a machine-parsing carrier reads the quote back harmlessly - so it needed
  no per-carrier opt-in.
- Unexecutable upload transports (resolved, PR #39): `sftp_upload` now has a
  paramiko backend, and the executor selects backends from a
  transport->uploader registry (matching the auth/field plugin pattern). A
  completeness test pins every schema-admitted upload transport to a
  backend, so an unbacked upload transport cannot enter the closed
  vocabulary in the first place - the "publishes then fails at trailer-close"
  gap is closed at build time. No publish-gate check was added: the closed,
  fully backed vocabulary leaves it nothing to catch.
- Carrier-config completeness at save/publish (resolved): `missing_config_keys`
  validates a definition's referenced `config.*` keys against the stored config
  and refuses publish (reporting them at save) when any are missing, so missing
  credentials surface at publish, not first booking.
- Unexecutable non-upload transports/content_types (resolved): the schema
  admitted `local_render` as a step transport and any content_type on http, yet
  the engine sends only http and the upload transports and encodes only json or
  form for http - so such a step rendered fine and first failed at send. The
  manifest and booking paths now mark that failed rather than 500, but it should
  never publish: `_transport_shape` refuses both at authoring (ADR 0009), skipped
  on lenient load so a pre-existing stored definition is not stranded.

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
  the dashboard, not embedded in it. In-process read-only tool-use loop, single-
  order diagnostics first (ADR 0016). Evals are owed before the trust threshold:
  a golden-scenario regression suite (reusing the shadow-mode replay-diff shape)
  gates any move to Teams or reliance by a non-operator; ships prompt-grounded
  with a human in the loop until then.
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
