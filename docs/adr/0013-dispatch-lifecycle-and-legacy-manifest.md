# 13. The dispatch lifecycle and the legacy manifest interface

Date: 2026-07-17

## Status

Accepted

## Context

ADR 0011 gave the legacy edge its create -> allocate -> paperwork lifecycle
(mock the first two, do the real domain work at paperwork). The remaining WMS
manifest operations - `markConsignmentsAsReadyToManifest` and `createManifest` -
are the next chunk, and mapping them onto the domain surfaced that NimbleShip's
notion of "dispatched" was imprecise.

The MetaPack manifest lifecycle the WMS (Solvitt) speaks:

- `markConsignmentsAsReadyToManifest(consignmentCodes)` marks specific
  consignments ready and returns a bare `true`. It is **selective** - Solvitt
  marks some allocated consignments ready and holds others back for a later
  manifest.
- `createManifest(carrierCode, warehouseCode, ...)` closes a manifest over the
  ready consignments for that carrier+warehouse and returns a manifest code
  (`DMC...`-style). Solvitt does not validate or reuse that code - it is
  fire-and-forget.

Grilling (2026-07-17) pinned the real domain meaning of dispatch, which the
existing code got subtly wrong:

- "Dispatched" means the goods have **physically left the warehouse (LDG)** -
  a fact about the goods, independent of which protocol edge entered the order.
- Only one carrier (Dachser) uses manifests. For a manifest carrier the goods
  leave when the **manifest is sent**. For every other carrier the business
  treats the consignment as dispatched the moment **labels are returned** (there
  is no manifest and no separate departure signal).
- NimbleShip's `create_manifests` stamped `dispatched` at manifest **creation**,
  before the send - marking a manifest carrier dispatched too early - and the
  JSON `dispatch-confirmations` endpoint dispatched non-manifest carriers only
  on an explicit call, which would record the same physical event at different
  moments depending on the edge.

## Decision

Dispatch timing is universal, determined by carrier type, not by edge:

- **Non-manifest carrier**: `dispatched` at paperwork, when the label is
  produced (`create_consignment`). Labels returned = gone.
- **Manifest carrier**: `dispatched` when its manifest is **sent**.

Two consignment states are added for the manifest path only:

- `ready_to_manifest` - recorded by `markConsignmentsAsReadyToManifest`
  (selective; unmarked consignments stay `allocated`).
- `on_manifest` - a pending `Manifest` has been created for it, but it has not
  yet been sent (so it is not yet `dispatched`).

`create_manifests` moves consignments to `on_manifest` (it previously set
`dispatched`); the manifest send is what sets `dispatched`. So a manifest
carrier's consignment is `allocated -> [ready_to_manifest] -> on_manifest ->
dispatched`, and a non-manifest carrier's is `allocated -> dispatched` at
paperwork.

The `dispatch-confirmations` endpoint (JSON) and the legacy `mark-ready` /
`createManifest` calls are therefore **manifest triggers**: they do work only
for manifest carriers. A non-manifest consignment is already `dispatched` from
paperwork, so a dispatch-confirmation that names one is a no-op, not an error.

The two legacy operations:

- `markConsignmentsAsReadyToManifest(codes)`: resolve each NS consignment code
  (via the staging row) to its domain Consignment and move `allocated ->
  ready_to_manifest`. Returns `true`.
- `createManifest(carrier, warehouse)`: sweep the `ready_to_manifest`
  consignments for that carrier+warehouse through `create_manifests`
  (-> `on_manifest`, a pending `Manifest`, a deferred send), and return a
  minted manifest code. A carrier that declares no manifest operation still has
  its consignments dispatched (they already were, at paperwork) and still
  returns a valid code.

Manifest codes are minted in-house from a database sequence, NS-native like the
consignment code (ADR 0011) - e.g. an `NSM` prefix. Solvitt does not validate
them. (The live 3PL proxy's separate `LDG` scheme is a different system; the
distinct prefixes are a feature during any parallel run.)

## Consequences

- "Dispatched" now means "physically gone", precisely and by carrier type, so
  the order timeline and any downstream read (tracking, "why hasn't this
  shipped") reflect reality rather than an edge accident.
- A manifest carrier's consignment is not `dispatched` until the carrier's
  manifest is actually sent; a failed or stalled send leaves it visibly
  `on_manifest`, never silently dispatched (this is why the send owns the
  transition, and why manifest-send failure handling must be loud - see the
  auth-failure fix that stopped a revoked credential bricking a manifest).
- The JSON `dispatch-confirmations` behaviour changes: a non-manifest
  consignment is now `dispatched` straight out of `create_consignment`, so it is
  not dispatched again on a confirmation call. Its tests change accordingly.
- Implementation is more than one PR: first the dispatch-lifecycle shift (the
  two new states, dispatch-at-send, non-manifest dispatch-at-paperwork, the
  dispatch-confirmations no-op), then the two SOAP operations and the manifest
  code minting on top.

## Clarification: how the manifest code is minted (2026-07-17)

The "database sequence" is a dedicated `manifest_codes` table whose
autoincrement id is the number in the code (`NSM0000001`), one row per
`createManifest` call - the same table-as-sequence trick the consignment code
uses on the staging row, but its own table rather than the `Manifest` id. The
reason it is not the `Manifest` id: `createManifest` must return a valid code
even when its sweep is empty (a carrier the WMS never readied anything for),
where no `Manifest` row exists to derive one from. The minted code is stored on
the `Manifest` it closes (`Manifest.code`, null for a manifest the JSON
dispatch-confirmation created, which returns no code) so the WMS-facing code and
the internal row are correlated. An empty sweep still mints and returns a code
but creates no `Manifest` and enqueues no send.
