# 15. Shadow mode: replay real WMS traffic and diff allocations

Date: 2026-07-18

## Status

Accepted

## Context

Phase 4 needs cutover credibility: evidence that when the live WMS (and later
Magento) traffic hits NimbleShip it produces the right outcomes, so the incumbent
(the 3PL proxy) can be switched off without allocations, labels, or paperwork
silently breaking. The roadmap also frames this as the evidence pack for the
workplace pitch.

The legacy SOAP edge is already built and tested against synthetic requests
(ADR 0002/0011/0012/0013). What is missing is the ability to run *real* recorded
traffic through NimbleShip and compare its behaviour to what the incumbent
actually did. The carrier side already has the analogue - CarrierTraffic
recording plus golden replay (ADR 0009) - so shadow mode is the WMS-edge version.

## Decision

Shadow mode is a **diff-and-review harness**, not a bug-for-bug clone. NimbleShip
is the successor and is deliberately better in places (it has already fixed real
incumbent bugs). So a run replays recorded incumbent traffic and **flags
divergences** for a human to bucket as match / deliberate improvement / real
regression. Regressions are fixed before cutover; improvements are part of the
pitch.

- **Semantic-first.** The diff is over domain outcomes (the allocation, then
  later the label, Parcels String, and paperwork), not the SOAP response bytes.
  Wire byte-match (the exact envelope the WMS parser accepts) is a separate,
  narrower dimension added later.
- **Offline replay**, not a live dual-run: recorded requests are replayed in
  batch, decoupled from the live system.
- **Side-effect-free, against a non-production database copy.** A replay must
  never book a carrier, write a label, or enqueue a manifest - it runs in a
  rolled-back savepoint, and it must stop before booking, whose commits run on
  separate sessions a rollback would not undo. The savepoint keeps rows out of
  the database, but it is not the whole story: staging mints a consignment code
  from an autoincrement id, and on Postgres that sequence advance is
  non-transactional (a rollback does not undo it), so replaying a batch would
  durably jump the id sequence. That is why shadow runs against a **copy** of the
  data, never live production - the sequence drift is harmless there (codes are
  opaque), and the copy is discarded. A scratch database is the deployment model,
  not the live one.
  NimbleShip's legacy allocation happens at paperwork (ADR 0011), which books, so
  the replay uses an `allocate_only` path: `create_consignment`'s pure allocation
  prefix, extracted so both it and shadow call the *same* logic, stopping before
  any booking. Shadow therefore always diffs the allocation NimbleShip really
  makes, never a drifting copy.
- **First slice: the allocation decision.** A recording holds an order's raw WMS
  SOAP (`createConsignments` + `allocateConsignments`) plus the incumbent's
  outcome. Replay drives those through the real legacy edge (so a divergence
  catches edge-translation bugs, not just allocation logic), runs `allocate_only`,
  and diffs `(allocated?, carrier, service)` against the incumbent's - only those
  dimensions; an error message is diagnostic (WMS-native text vs ours, always
  differing) and is not compared, or every mutual decline would read as a false
  divergence. A NimbleShip fault is itself always a divergence, though: it is a
  gap worth surfacing, even when the incumbent also declined. A wrong
  carrier is the outcome that actually falls over on cutover, and this proves the
  record -> replay -> diff -> report loop end to end.
- **Grounded on real traffic, built synthetic-first.** Golden recordings are the
  incumbent's real WMS request/response pairs; the harness is built and tested
  against synthetic recordings in that format first, with real captures
  conforming to it.

## Consequences

- `allocate_only` is now a shared, side-effect-free allocation entry point, so
  shadow and production allocation can never diverge in what they compute.
- The recording format carries raw SOAP plus the incumbent outcome, extensible to
  the later label/Parcels String/paperwork slices without a reshape.
- Divergences are surfaced, not auto-failed: the report categorises them for
  review, because some are the successor being deliberately correct.
- Parcels String + label slice (added): `replay_paperwork` extends the loop for
  a local-render carrier (dropout) that makes no carrier call - producing the
  label into an in-memory store, so nothing writes to disk - and diffs the
  Parcels String and that a valid label was produced. It does not byte-diff the
  PDF: the incumbent and NimbleShip render different PDFs from the same data, so a
  byte match would false-diverge on every order; the shipping-critical data is
  the barcodes (the Parcels String).
- Live-API carrier slice (rung 1 built - Furdeco; rungs 2-3 designed): extends the
  paperwork diff to booking carriers (Furdeco, Dachser, ...) without a live carrier
  call.
  - **Replay the recorded carrier response through the real edge.** The golden
    recording carries the carrier's own book response; replay feeds it back
    through NimbleShip's real book-step execution via a mock transport, so the
    diff catches NimbleShip's own response-parsing, label-extraction, and
    barcode-mapping bugs - not just the allocation. Recording only the incumbent's
    final outcome would leave that translation layer undiffed.
  - **Side-effect-free by construction, not by scratch-discard.** The booking path
    deliberately commits per-step carrier traffic (and, for SSCC carriers, the
    client-minted allocations) on separate sessions so they survive a crash - the
    opposite of what replay needs. Rather than let those commit and discard a
    scratch database (which makes safety a deployment discipline and mixes two
    isolation models), the traffic recorder and the SSCC source become injected
    collaborators defaulting to today's real committers; shadow overrides them
    with an in-memory traffic sink and a recorded-SSCC source, keeping the one
    rolled-back-savepoint isolation model across every slice.
  - **Recorded identifiers, uniformly.** Client-minted SSCCs (minted before the
    call, so NimbleShip's would differ from the incumbent's) are fed from the
    golden, exactly as carrier-returned barcodes are - the diff stays an exact
    match and NimbleShip's deterministic mint (prefix, check digit, one-per-parcel)
    stays covered by its own tests and publish gate, not re-verified by shadow.
  - **Three independent diff dimensions**, each a separate extraction the WMS
    consumes on its own: the label (byte-for-byte for `base64_pdf`, where both
    sides decode the same carrier PDF, so a byte match is meaningful; a boolean
    for `local_render`, where the renderers differ), the Parcels String, and the
    tracking reference. A byte-perfect label proves only the `label_pdf` path; a
    wrong `tracking_reference` mapping is invisible to it.
  - **Proving ladder:** Furdeco first (http-book, local-render, no SSCC - all the
    architectural risk, none of the feature complications), then a synthetic
    `base64_pdf`-no-SSCC rung to isolate the byte-diff, then Dachser (base64_pdf +
    fed SSCCs). FedEx (`png_pages`) and PalletForce (`fetch_step`) are out of scope
    until those label sources are implemented in the booking path at all - shadow
    would flag them as divergences (NimbleShip faults where the incumbent
    labelled), a real gap, but no match is possible until then.
- Deliberately deferred (grill just-in-time): the wire SOAP byte-match; Magento
  checkout diffs; the real-traffic capture mechanism (how the incumbent's pairs
  are logged); and any review UI beyond a batch report.
