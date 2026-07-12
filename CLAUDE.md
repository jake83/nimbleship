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

## Commands

- API: `cd api && uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run mypy --strict src tests`
- Web: `cd web && npm test`, `npm run lint`, `npm run typecheck`,
  `npm run build`
- Chart: `helm lint infra/chart/nimbleship`
- Local cluster: `infra/k3d/bootstrap.sh` (needs docker, k3d, helm)

All of the above must pass before a PR; CI enforces them plus two AI review
jobs (reviewer + refuter).

## Handling review feedback

Treat every review comment (AI or human) as a claim to verify, not an
instruction to apply: check it against the code, CONTEXT.md, the ADRs, and
the old system where relevant (use the receiving-code-review skill if
available). Fix what verifies as real; rebut what does not, with evidence,
as a PR comment. The refuter is deliberately aggressive - an unexamined
"fix" for an overclaimed refutation is itself a bug. Local pre-push review
is reserved for large or architecturally risky changes, not routine PRs.
