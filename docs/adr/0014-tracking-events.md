# 14. Tracking Events: a normalised store fed by source adapters

Date: 2026-07-18

## Status

Accepted

## Context

NimbleShip needs to ingest carrier tracking signals ("in transit", "delivered",
"failed attempt") so the order timeline and the "why hasn't this shipped /
where is it" reads reflect reality, and so the AI assistant answers from
structured data rather than log archaeology. The first source is the Voila
aggregator's webhook (the incumbent already forwards carrier tracking through
it); direct-carrier sources come later.

Two shaping questions:

- Where do tracking signals live? They are high-volume external facts with their
  own shape (a carrier status code, a source shipment id, per-parcel codes, the
  source's own event id and timestamp), a different lifecycle from the internal
  append-only OrderEvent timeline that is the system's spine.
- Every carrier/source speaks its own status vocabulary (Voila alone has ~20
  numeric codes). A downstream read must not re-implement that mapping.

## Decision

- Tracking Events live in a **dedicated store** (`tracking_events`), separate
  from the OrderEvent timeline, linked to orders by `order_number`. The timeline
  stays lean; tracking gets its own indexed store.
- Each event keeps the source's **raw status code** verbatim (for audit and
  remapping) alongside a **canonical status** normalised at ingestion. The
  canonical vocabulary is small and carrier-neutral: `in_transit`,
  `out_for_delivery`, `delivered`, `exception`, `returned`, `unknown`. A raw
  code with no mapping normalises to `unknown` - loud and queryable, never
  silently dropped.
- Ingestion is through **per-source adapters** behind a webhook endpoint
  (`POST /api/tracking/webhooks/{source}`): the adapter parses the source's
  payload and applies its status map. New sources register an adapter without
  touching the endpoint (the same closed-vocabulary-with-a-seam pattern as the
  carrier definition engine, ADR 0009). Ingestion is **idempotent** on
  `(source, external_id)` - a redelivered webhook stores nothing new.
- The webhook is **closed until the source's secret is configured** (the same
  never-open-by-omission stance as the Legacy Interface).

## Consequences

- Downstream reads (tracking pages, the AI assistant, "why hasn't this shipped")
  query one canonical vocabulary, indexed by order, while the raw code survives
  for audit and for re-deriving the mapping later.
- A new source is an adapter plus a status map, not a schema or endpoint change.
- The per-source status map is configuration, not a hard contract: refining a
  code's canonical bucket is a data change, reviewable, not a migration.
- Deliberately deferred (grill just-in-time): the read/query API and the
  tracking UX; whether a status change also appends a summary to the OrderEvent
  timeline; dedup subtleties beyond the idempotency key (an event redelivered
  with changed data is currently a skip); and matching each source's real
  webhook auth scheme at shadow mode (a shared secret stands in for now).
