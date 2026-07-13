# NimbleShip

Carrier management, rebuilt: the successor to the 3PL proxy. NimbleShip sits
between sales/warehouse systems and delivery carriers - deciding how
consignments ship, producing labels and paperwork, and answering for every
decision it makes. AI-assisted carrier onboarding and routing configuration
are first-class features; the dispatch path never depends on AI availability.

## Layout

- `api/` - FastAPI backend (Python 3.12, uv, SQLAlchemy, Postgres)
- `web/` - React + TypeScript frontend (Vite, shadcn/ui, Tailwind)
- `infra/` - Helm chart and k3d bootstrap for local Kubernetes
- `docs/` - roadmap and architecture decision records
- `CONTEXT.md` - the domain glossary; canonical language for this repo

## Getting started

Backend (requires [uv](https://docs.astral.sh/uv/)):

```sh
cd api
uv sync
uv run pytest          # tests
uv run uvicorn nimbleship.main:create_app --factory --reload
```

Background jobs (manifest sending) run on a Postgres-backed queue consumed
by a separate worker process - it needs a `postgresql://` database, so it
is usually exercised via the k3d stack below:

```sh
uv run procrastinate --app=nimbleship.queue.queue_app worker
```

Frontend (requires Node 24+):

```sh
cd web
npm install
npm test               # tests
npm run dev
```

Full stack on local Kubernetes (requires docker, k3d, helm):

```sh
infra/k3d/bootstrap.sh
# then browse http://nimbleship.localhost:8080
```

## How this repo works

Read `CLAUDE.md` for conventions, `docs/ROADMAP.md` for the plan, and
`docs/adr/` for the decisions behind the architecture. Development is
test-driven; CI (lint, types, tests, image builds) plus an AI adversarial
review gate every pull request.
