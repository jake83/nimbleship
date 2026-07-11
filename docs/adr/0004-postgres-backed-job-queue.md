# 4. Postgres-backed job queue instead of Celery/Redis

Date: 2026-07-11

## Status

Accepted

## Context

The old 3PL system is entirely synchronous: carrier calls happen in-request
(forced, where the WMS blocks waiting for base64 labels in the response) and
everything else runs on cron. NimbleShip adds work that cannot live in a
request cycle: manifest sending with retries, tracking ingestion, and
long-running AI jobs (integration building, rule drafting) that take minutes.

The Python default is Celery with Redis (or RabbitMQ) as broker. That adds a
stateful piece of infrastructure to deploy, monitor, and upgrade on K3s, and
broker-based jobs are not transactional with database writes - a crash between
commit and enqueue loses or duplicates work. Expected volumes are a single
warehouse operation's dispatch traffic, orders of magnitude below where a
dedicated broker earns its keep.

## Decision

Background work runs on a Postgres-backed job queue (Procrastinate or
equivalent), consumed by a dedicated worker deployment. Jobs are enqueued in
the same transaction as the domain writes that cause them. The hot path - the
WMS waiting for allocation and labels - stays synchronous in-request.

## Consequences

- No broker to operate: the queue lives in the database that already exists,
  which keeps the K3s footprint at web + worker + Postgres.
- Enqueue-with-commit atomicity: a consignment and its manifest job cannot
  disagree about whether the other happened.
- Queue throughput is bounded by Postgres, which is comfortably sufficient at
  this scale; if that ever changed, jobs are isolated behind the queue
  abstraction and a broker could be introduced per queue.
- AI jobs get first-class status tracking (queued, running, awaiting-user,
  done) via ordinary rows, which the UI can subscribe to.
