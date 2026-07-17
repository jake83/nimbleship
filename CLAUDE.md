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

Every change follows this loop, except a trivial PR (defined in step 4),
which takes the minimal path described there instead of the adversarial
pass. Step 1 applies only to fat PRs, as defined in step 1 itself:

1. (fat PRs only) Local code review before pushing; act on design-level
   findings while they are cheap. A PR is "fat" when it establishes
   patterns or restructures things - roughly the first PR of a phase - OR
   when it touches domain logic in more than one place (learned on PR #22,
   which spanned three domain areas, skipped local review, and then had
   its pipeline pass silently no-op on a usage limit: the two gates can
   fail together, so neither may assume the other).
2. Push and open the PR. CI plus the reviewer and refuter jobs run. They take
   several minutes - do not idle waiting on them: start the next step while they
   run (stacked on this branch if it depends on this PR, off main if not), and
   return to triage when the review settles, then rebase the ahead-work onto the
   merged base. The review wait thus overlaps real work instead of blocking it.
   Caveat: a review that materially changes THIS PR forces rework of anything
   built on it, so prefer parallelising a genuinely independent next step and
   accept the occasional rebase for a stacked one (owner-granted 2026-07-17).
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
     type-trivial changes get human eyes and keep the full loop. Within it, a
     trivial PR takes the MINIMAL path instead of the AI adversarial pass
     (steps 2-3, wasteful on this class): a local code-review at medium effort
     before pushing (standing in for the pipeline), then the `trivial` label -
     which skips the reviewer/refuter jobs while ci.yml (lint/type/test/build)
     still runs - and merge on green. Apply the label AT creation
     (`gh pr create --label trivial`): the reviewer/refuter jobs check it when
     the PR event fires, so a label added after they start does not stop them.
     The label is the ONLY thing that skips AI review; never apply it outside
     this definition, and when in doubt drop it and take the full loop.
   - **Everything else**: the agent self-merges once the AI review loop has
     settled - CI green (ci.yml lint/type/test/build), the reviewer and
     refuter jobs run, and every finding triaged per "Handling review
     feedback" and the two-round rule (fixed, rebutted, or tracked as a
     follow-up in the wrap-up). When a reviewer or refuter job FAILS (usage
     limit or transient), re-trigger it once - `gh run rerun --failed <run-id>`
     - before doing anything else: a first failure is usually a usage limit
     that clears on a retry. Self-merge needs a GENUINE review signal, not
     merely "nothing blocked": at least ONE of the reviewer or refuter must
     have completed a real pass. A usage-limited job that no-ops is NOT a pass
     - if, after the one retry, BOTH are still down (the two-gates-fail-together
     mode PR #22 hit once), run a local code-review at medium effort as the
     stand-in rather than merge on no signal. One genuine pass, per "a clean
     machine pass is not required", is enough; zero is not. Squash-merge and
     delete the branch.
     This authority is owner-granted (2026-07-15) to keep development moving
     without a human merge gate, and is revocable: the owner can reinstate
     the gate at any time. It REPLACES the former "an agent NEVER merges"
     law for as long as it stands. What still stops for the human is a
     QUESTION, never a merge: a design decision, an ADR-level trade-off, a
     grilling session, anything needing an answer the code and this file
     cannot supply - surface it and wait. When unsure whether a finding is
     settled or needs a human call, do not merge; ask. No GitHub review
     approval exists on a solo repo, so the squash-merge commit itself
     records the sign-off, with the wrap-up comment as its rationale.
   - **Low-stakes auto-merge** (owner-granted 2026-07-15): a PR whose every
     change is a non-governance documentation file - a README or a doc that is
     not CLAUDE.md, CONTEXT.md, an ADR, or docs/ROADMAP.md, and never source,
     tests, schema, or an API contract - may be queued for GitHub auto-merge at
     creation (`gh pr merge <n> --auto --squash`), so it lands the moment the
     required CI checks pass without a triage read. The AI review still runs; a
     finding it posts after the merge rides a follow-up PR. Test-only PRs are
     deliberately excluded: CI cannot tell an intact test from one with a
     silently dropped assertion (this repo has hit that three times), and the
     review that can is non-blocking, so tests keep the read-then-merge flow.
     A commit pushed after `--auto` merges is stranded and rides a new PR, like
     any post-merge work (one pushed while it still waits is safe - `--auto`
     re-arms on the new head). Past ~200 changed lines, take read-then-merge
     anyway. When in doubt about the class, it is not low-stakes: use
     "Everything else". The required
     checks (the ci.yml jobs, not the usage-limiting reviewer/refuter) and the
     "Allow auto-merge" toggle are applied by `.github/setup-branch-protection.sh`.
     ci is required for everyone (enforce_admins), so ci-green is an inviolable
     precondition - no one, agent included, merges red ci. Because the AI jobs
     are NOT required checks, a triaged logic PR still merges on ci-green with a
     plain `gh pr merge --squash`, even when a usage-limited reviewer/refuter
     left its own non-required check red. Green checks alone never justify a
     merge outside this class, because a passing refuter still posts findings
     that must be triaged (PR #69 did).
   Stacked PRs: merge the base PR and DELETE its branch first so GitHub
   retargets the stacked PR to main - otherwise it merges into a dead
   branch and its content silently never reaches main.
   Finalize once, then merge: get ALL intended content into a PR before
   merging it, because a push that races the merge is squash-dropped silently
   (an ADR note on PR #39, a CLAUDE.md convention on PR #40, and a test fix on
   PR #60 all stranded this way - each merged without its trailing commit).
   Once a PR is merged, anything further rides a NEW PR, never a late push to
   the merged branch. The race is real even under self-merge: a human may merge
   a handed-off or in-flight PR out of band (as happened to PR #60), so before
   pushing a follow-up commit, confirm the PR is still open. After any merge,
   verify the intended commits actually reached main (grep main for the change)
   rather than assuming the merge carried them - it is the only reliable catch
   for this race and for the dead-branch case above.
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
4. **The wrap-up terminates the loop**: after round 2 (or a blocking round),
   the agent posts the wrap-up - what was fixed, what was rebutted, what is
   tracked - and then self-merges per the development loop's step 4
   owner-granted authority (a clean machine pass is not required and not waited
   for). It goes to the human instead only when a QUESTION remains open, per
   that same step 4.

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
