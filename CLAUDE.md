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
- Every comment and docstring must earn its place: state a non-obvious
  constraint or the "why" the code cannot show, in as few words as do the job.
  One tight sentence beats a paragraph. A comment that only restates what the
  code does, or repeats the docstring, is deleted - not shortened; prefer no
  comment to a fluffy one. (Re-sharpened after PRs #47 and #50-#52 shipped
  first cuts that over-explained.)
- Verify authored content against the code, not just review feedback. Any
  executable instruction or format-specific example you write - a shell
  command, config snippet, sample payload, or "the format is X" claim in a
  doc, ADR, comment, or PR body - is a claim to verify before commit: run its
  real output through the code that consumes it (the parser, endpoint, or
  schema). An unexecuted runbook is unverified. This is the same standard
  "Handling review feedback" demands for incoming claims, applied to what you
  author (learned on PR #40, where an ADR runbook told operators to store raw
  `ssh-keyscan` output that the `sftp_host_key` parser rejects - it was
  asserted, never run through the parser).

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

Every change follows this loop; step 1 applies only to fat PRs, as
defined in step 1 itself:

1. (fat PRs only) Local code review before pushing; act on design-level
   findings while they are cheap. A PR is "fat" when it establishes
   patterns or restructures things - roughly the first PR of a phase - OR
   when it touches domain logic in more than one place (learned on PR #22,
   which spanned three domain areas, skipped local review, and then had
   its pipeline pass silently no-op on a usage limit: the two gates can
   fail together, so neither may assume the other).
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
   Finalize once, then hands off: get ALL intended content into a PR before
   posting `needs-human-review`, because a push that races the human's merge
   is squash-dropped silently (it happened twice - an ADR note on PR #39 and
   a CLAUDE.md convention on PR #40, each merged without its trailing commit).
   Anything that arises after handoff rides a NEW PR, never a late push to the
   handed-off one. After any merge, verify the intended commits actually
   reached main (grep main for the change) rather than assuming the merge
   carried them - it is the only reliable catch for this race and for the
   dead-branch case above.
   The trivial definition starts deliberately tight and is loosened only
   with evidence: when human review of a class of PRs has stopped finding
   anything for a sustained stretch, widen the definition here and record
   why in the same commit.
5. Route every HUMAN review finding into exactly one artifact, so the same
   finding never needs a human twice: coding convention -> CLAUDE.md; domain
   language -> CONTEXT.md; architectural decision -> new or amended ADR; a
   bug class the AI reviewers should have caught -> the reviewer/refuter
   prompts in .github/workflows/claude-review.yml.

## Resolving merge conflicts

Union-merging both-sides-added tests is the right instinct but has a known
hazard: conflict boundaries can cut a test mid-function, silently dropping
its trailing assertions (it struck three times during the Phase 2 merges;
the unused-variable lint caught it each time). After any union resolution,
check every touched test still asserts, and never pipe a gate command into
anything that swallows its exit code.

## Handling review feedback

Treat every review comment (AI or human) as a claim to verify, not an
instruction to apply: check it against the code, CONTEXT.md, the ADRs, and
the old system where relevant (use the receiving-code-review skill if
available). Fix what verifies as real; rebut what does not, with evidence,
as a PR comment. The refuter is deliberately aggressive - an unexamined
"fix" for an overclaimed refutation is itself a bug.

## When the review loop ends: the two-round rule

The fix-review cycle is capped by structure, not exhaustion (amended
2026-07-13 after PR #9 ran five rounds and later loops showed round 3+ is
almost always diminishing returns - while the round-2 pass over the FIXES
caught fix-introduced bugs three separate times, so it stays):

1. **Round 1**: the original push gets the full adversarial pass. Triage
   every finding (verify, fix, or rebut) and land all fixes as ONE
   consolidated push - drip-feeding fixes spins wasted pipeline rounds.
2. **Round 2**: the pass over the fixes. Fixes are new code written faster
   than the original and are MORE bug-prone per line - this verification
   round has repeatedly caught fix-introduced bugs and false "fixed"
   claims. Triage its findings with the default flipped: everything
   non-blocking becomes a tracked follow-up in the wrap-up comment, never
   a new fix push.
3. **Blocking exception**: only a genuine blocking finding against the
   current tip (real correctness or security, including silent-wrong
   behaviour) earns another consolidated fix push and one more
   verification round. This should be rare.
4. **The human is the terminator**: after round 2 (or a blocking round),
   the PR goes to the human with the wrap-up in hand - what was fixed,
   what was rebutted, what is tracked. The human's merge closes the loop;
   a clean machine pass is not required and not waited for.

Standing rules that survive the amendment:
- Findings against superseded commits are rebutted with the fix reference
  and never count as rounds.
- Comments are free: triage and rebuttals never trigger re-review - only
  pushes do. Never push comment-only or cosmetic changes into an open
  loop; they ride the next real PR.
- Run gates BARE, never piped: `uv run pytest && uv run ruff check .` -
  piping a gate into tail/grep/head swallows its exit code, and this has
  let red commits through twice. Filter output only AFTER the bare run
  has passed.
