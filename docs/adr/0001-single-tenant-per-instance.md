# 1. Single tenant per instance

Date: 2026-07-11

## Status

Accepted

## Context

NimbleShip is a greenfield rebuild of the 3PL proxy. It will be hosted by one
workplace, is a hobby/portfolio project, and there is no plausible path to a
second tenant sharing one deployment. Multi-tenancy was considered (it is how
Metapack and Sorted Pro are built) because tenancy is famously expensive to
retrofit into a shared database.

However, the deployment direction is containerised (K3s). That makes the
retrofit argument moot: a second company would get a second instance - its own
namespace and database - rather than a row in a tenants table. Shared-database
tenancy would tax every table, query, test, and AI-assistant tool with tenant
scoping that would almost certainly never be exercised.

## Decision

NimbleShip is single-tenant: one instance serves one company. There is no
tenant entity and no tenant scoping in the schema or code. Scaling to another
company means deploying another instance.

One discipline is kept: no company-specific facts in code. Everything specific
to the operating company (carriers, credentials, rules, warehouse details,
branding) lives in the database or configuration, so a fresh install is a
deploy plus configuration, never a fork.

## Consequences

- Simpler schema, queries, auth, and AI-assistant data tools; no tenant
  context to propagate.
- The AI assistant can read the whole database without cross-tenant leak
  concerns.
- If shared-database SaaS ever became a genuine goal, that would be a major
  migration - accepted as a risk that is very unlikely to materialise.
- Demo/portfolio installs are just local instances with seed data.
