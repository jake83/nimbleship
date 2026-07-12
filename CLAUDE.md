# NimbleShip - agent instructions

NimbleShip is a carrier management system: the greenfield successor to the
3PL proxy (Laravel/Vue, at ~/PhpstormProjects/3pl-proxy, read-only reference).
Single tenant per instance. One domain core, two protocol edges (JSON API is
canonical; a legacy SOAP edge translates the WMS dialect and contains no
business logic).

## Before writing code

- `CONTEXT.md` is the domain glossary. Use its terms exactly; never introduce
  a synonym for a defined term.
- `docs/adr/` holds the architecture decisions. Do not contradict an ADR
  casually; if one seems wrong, raise it instead of coding around it.
- `docs/ROADMAP.md` is the phase plan.

## Conventions

- TDD, strictly: red (failing test first), green (minimal pass), refactor.
  Tests pin external behaviour (payloads, labels, allocations), never
  implementation details.
- Python: uv, ruff (lint + format), mypy --strict. Everything typed,
  including tests. FastAPI routes live under `/api`.
- TypeScript: strict mode, oxlint, vitest. Components are shadcn/ui on
  Tailwind; add components via `npx shadcn add <name>` in `web/`.
- No company-specific facts in code - carrier names, credentials, rules, and
  warehouse details are data, never constants.
- Commit messages: imperative subject, body explains why. Never add an agent
  co-author line.
- Comments and docstrings state present-tense constraints the code cannot
  show - never provenance. No "the old system", "3PL", legacy class names,
  or "ported from X" in code: a future reader inherits this codebase without
  that context. Provenance lives in commit messages, PR bodies, and ADRs
  (historical documents by nature); CONTEXT.md carry-over notes are fine.
  PR/finding references (e.g. "refuter, PR #9") are acceptable only as a
  trailing pointer AFTER the constraint itself is stated in full.

## Commands

- API: `cd api && uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy --strict src tests`;
  schema is owned by Alembic: `uv run alembic upgrade head` (dev setup),
  `uv run alembic revision --autogenerate -m "..."` (schema changes)
- Web: `cd web && npm test`, `npm run lint`, `npm run typecheck`,
  `npm run build`
- Chart: `helm lint infra/chart/nimbleship`
- Local cluster: `infra/k3d/bootstrap.sh` (needs docker, k3d, helm)

All of the above must pass before a PR; CI enforces them plus two AI review
jobs (reviewer + refuter).

## The development loop

Every change follows this loop; step 1 applies only to PRs that establish
patterns or restructure things - roughly the first PR of a phase:

1. (fat PRs only) Local code review before pushing; act on design-level
   findings while they are cheap.
2. Push and open the PR. CI plus the reviewer and refuter jobs run.
3. Triage every AI finding per "Handling review feedback" below; push fixes,
   post rebuttals. The pipeline re-runs on each push until settled.
4. When the loop is settled, post a wrap-up comment ("AI loop complete:
   N findings, M fixed, K rebutted"), then:
   - **Trivial PR** - judged by change TYPE, not size. Every change in the
     PR must be behaviour-preserving and of one of these kinds: a pure
     rename, a comment/docstring/typo fix, a cosmetic frontend tweak (copy,
     spacing, styling, markup rearrangement with no logic change), or an
     equivalent no-behaviour-change edit. Nothing touching domain logic,
     schema, API contracts, workflows, dependencies, CONTEXT.md, or ADRs is
     ever trivial, however small. Size backstop: past ~25 changed lines even
     type-trivial changes get human eyes. Apply the `trivial` label; the
     agent may merge on green.
   - **Everything else**: apply `needs-human-review` and assign the repo
     owner (GitHub cannot request a review from the PR author, so the flag
     is label + assignee). An agent NEVER merges a non-trivial PR - this is
     law, not enforced by GitHub, so it must never be broken. When in doubt,
     a PR is not trivial. The human's merge IS the approval: no GitHub
     review approval exists on a solo repo (an author cannot approve their
     own PR); findings arrive in conversation or PR comments, and the merge
     commit records the sign-off.
   Stacked PRs: merge the base PR and DELETE its branch first so GitHub
   retargets the stacked PR to main - otherwise it merges into a dead
   branch and its content silently never reaches main.
   The trivial definition starts deliberately tight and is loosened only
   with evidence: when human review of a class of PRs has stopped finding
   anything for a sustained stretch, widen the definition here and record
   why in the same commit.
5. Route every HUMAN review finding into exactly one artifact, so the same
   finding never needs a human twice: coding convention -> CLAUDE.md; domain
   language -> CONTEXT.md; architectural decision -> new or amended ADR; a
   bug class the AI reviewers should have caught -> the reviewer/refuter
   prompts in .github/workflows/claude-review.yml.

## Handling review feedback

Treat every review comment (AI or human) as a claim to verify, not an
instruction to apply: check it against the code, CONTEXT.md, the ADRs, and
the old system where relevant (use the receiving-code-review skill if
available). Fix what verifies as real; rebut what does not, with evidence,
as a PR comment. The refuter is deliberately aggressive - an unexamined
"fix" for an overclaimed refutation is itself a bug.

## When the review loop ends

The fix-review cycle terminates by rule, not by luck (established on PR #9,
which took five substantive rounds):

- **Settlement rule**: the loop closes when a pass yields zero new REAL
  findings against the CURRENT tip. Findings against superseded commits are
  rebutted with the fix reference and do not count - but a real finding
  against the tip reopens the loop, always, even after settlement was
  declared.
- **Severity floor**: non-blocking findings (coverage gaps, wording nits,
  bounded follow-ups) become tracked follow-ups in the wrap-up comment, not
  new fix-pushes. Deliberately ending the cycle is allowed; the human merge
  ratifies "good enough".
- **Comments are free**: triage and rebuttal comments never trigger
  re-review - only pushes do. Never push comment-only or cosmetic changes
  into a settling loop; they ride the next real PR.
- The human is the fixed point: total rounds are bounded by real bugs, and
  any disagreement the machines cannot settle ends at the human merge gate.
