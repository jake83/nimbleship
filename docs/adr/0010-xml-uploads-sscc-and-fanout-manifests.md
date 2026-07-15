# 10. XML uploads, client-minted range numbers (SSCC), and per-consignment manifests

Date: 2026-07-13

## Status

Accepted

## Context

ADR 0009 established the declarative carrier-definition engine and named
Dachser as the proving ladder's final, hardest rung: "SSCC + SFTP EDI +
DigiDocs, its own mini-epic." Fagans has since proved the `ftp_upload`
transport with CSV file rendering (PR #36). Building Dachser against the
real integration surfaced three capability gaps the vocabulary cannot yet
express, each a genuine design decision rather than a mechanical addition.

Dachser's shape is also unlike every other carrier and worth stating,
because it drives the rest:

- **Book is "get the label," not "tell the carrier."** At the packing
  bench, NimbleShip calls Dachser's REST `/labels` endpoint (sending the
  parcels' SSCCs) and receives a base64 PDF the warehouse prints and
  applies. No shipment is declared here.
- **The manifest is the shipment declaration, as an EDI file.** When the
  trailer doors close (scan-out), the shipment advice is transmitted: one
  `ForwardingOrderInformation` XML file **per order**, dropped on SFTP,
  fire-and-forget. Dachser calls this "EDI transmission"; it is exactly the
  Manifest concept (CONTEXT.md), and the manifest infrastructure (PR #34)
  triggers it.

The old system's REST transport-order `/send` is an alternative ("Voila")
path we do not adopt; its DigiDocs / commercial-invoice feed is deferred -
it was never scheduled in the old system and depends on a customs-paperwork
subsystem NimbleShip has not built.

## Decision

### 1. XML as an upload file format

Upload steps gain `content_type: "xml"` alongside `csv`, upload-only (no
http-xml body until a carrier needs it). An xml upload step declares a
`root_element` naming the single document wrapper; the renderer emits a
fixed `<?xml version="1.0" encoding="UTF-8"?>` prolog (no `standalone`
unless a carrier is shown to require it).

The document is the same declarative mapping the engine already renders -
`target` dot-paths build nested elements, `each`-loops over a collection
become repeated same-name elements (Dachser's per-parcel `ShipmentLine`,
`PackageIdentification`). The one new token: an **`@`-prefixed terminal
segment is an attribute** of its parent element (`ShipmentAddress.@AddressType`
-> `<ShipmentAddress AddressType="...">`), validated at authoring as
terminal-and-scalar. This is the badgerfish/xmltodict convention; it keeps
the format data, not a template language (the ADR 0009 line), and reuses
the existing target machinery. Attributes only - no namespaces or mixed
content until a carrier needs them.

### 2. Client-minted numbers within a carrier-provisioned range (SSCC)

An **SSCC** (CONTEXT.md) is a GS1 unit identity NimbleShip mints by
incrementing a bounded suffix within a range the carrier provisions (the
prefix, held in Carrier Config), plus a GS1 mod-10 check digit. Two
additions to the number-range machinery (ADR 0009's field-plugin / allocator
cluster):

- **A `halt` exhaustion policy** on the allocator, beside the existing
  `wrap`. SSCC uses `halt`: reaching the suffix limit raises
  `RangeExhausted` (wrapping would reissue a live code). Exhaustion is a
  loud `booking_failed` - "request a new range."
- **The range refresh is a config prefix change**, made safe by keying the
  SSCC sequence on the current prefix (the sequence name includes it).
  Provisioning a new range is just updating the prefix: allocation sees a
  fresh sequence and starts at 1, and the spent prefix's counter is frozen
  in the table as an audit of what was issued. No separate reset action to
  forget, and a spent range cannot be resumed by accident.

SSCC assembly (prefix + zero-padded suffix + check digit) lives in one
routine, `assemble_sscc`; the mod-10 algorithm is the only new engine code.
(An initial render-time computed-field-plugin form, mirroring
`AllocatedNumberField`, was superseded by the dispatch mint below and
removed.) A **soft threshold warning**
("range running low") is emitted as a structured log with a queryable
remaining-count; delivering it (email/Teams) is deferred to Phase 7
observability - the dispatch path must not depend on a notification channel.

**Minting is declared, not hardcoded, and lands on the parcel.** The book
operation carries an `allocate` block - `{kind: "sscc", per: "parcel",
prefix: "config.<key>", policy: "halt"}` - and the dispatch reads that
declaration to know it must mint, so no carrier name lives in code. Before
the carrier call it mints one SSCC per parcel and stores the assembled code
on the parcel's `carrier_barcode`; the book render and the fan-out manifest
then both read it as `item.carrier_barcode`, so one stored value feeds both
and mint-time and read-time never diverge. `assemble_sscc` is the single
assembly routine. The schema pins the
block: at most one entry (a parcel has one carrier barcode), `book`-only, a
`config.*` prefix, and `halt` for `sscc` (a wrapping SSCC would reissue a
live code). Minting commits in **its own transaction** (like the traffic
rows): the allocation lock releases before the carrier call rather than
being held across its latency, and a halt-range number is durably spent the
instant it is issued - a crash can never reissue a code that may already
have reached the carrier, at the cost of wasting the numbers of a booking
that later fails. A range exhausted partway through a consignment mints
nothing (all parcels or none) and fails the booking loudly.

### 3. Per-consignment (fan-out) manifests

Manifests so far send one document per manifest (all consignments in a
single declaration). Dachser's manifest is one EDI file **per order**, so a
manifest operation may **fan out**: its step renders and sends once per
consignment in the manifest, from that consignment's own shipment facts
(including the SSCCs stored at booking), rather than once from the batch's
`manifest.*` facts. The Manifest concept is unchanged - the per-carrier
declaration of what physically left, at scan-out; only its emission shape
(N documents vs one) is per-carrier, as CONTEXT.md's Manifest entry already
allows.

### 4. SFTP host-key pinning, fail-closed

SFTP authenticates the client to the server (password) and the server to the
client (host key). Skipping the second half leaves the credentials and the
EDI exposed to anyone who can answer on the carrier's host:port - the exact
MITM that host keys exist to stop, and a regression the FTP transport never
had anything to lose to. So the server is pinned: the carrier's expected host
key lives in Carrier Config as `sftp_host_key` (one OpenSSH public-key line),
is passed to the connection, and a server presenting a different key is
refused. Pinning is **fail-closed**: a missing or unparseable pin refuses the
upload rather than connecting unverified, because an unverified connection is
the very failure being prevented. The alternative, trust-on-first-use, is
rejected - it trusts whatever answers the first time, which on a fresh install
is precisely when an attacker would substitute a host. The cost is that
onboarding a carrier must obtain its host key; that is the right cost.

Obtaining `sftp_host_key` at onboarding: it is not a credential the carrier
issues but the public key the carrier's SFTP server presents, captured once
and pinned. The field takes a two-field public-key line, `<type> <base64>`.
`ssh-keyscan -t ed25519,ecdsa,rsa <sftp_host>` prints the server's keys in
`known_hosts` format, `<host> <type> <base64>`, so drop the leading host
field before storing - e.g. `ssh-keyscan ... | cut -d' ' -f2,3` - then pick
one line. (The pin fails closed, so pasting the raw hostname-prefixed line is
rejected as unparseable rather than silently mis-stored.) The trust step is
verifying that captured key is really the carrier's, out of band, before
pinning: compare its fingerprint (`ssh-keygen -lf`) against a fingerprint the
carrier publishes in its onboarding pack, or confirm it with the carrier's
support. Where a carrier publishes no fingerprint, capture-on-first-connect
then confirm the fingerprint with support is the fallback - weaker than a
published fingerprint, but still one verified capture pinned thereafter. A
carrier rotating its server key is a config update, not a code change; the
upload fails loudly against the stale pin until the new key is stored, which
is the right failure direction.

## Consequences

- The upload vocabulary now spans two file formats behind one closed set of
  content types; a third (a fixed-width EDIFACT, say) is a new renderer, not
  a plugin - expressiveness pressure still surfaces as a reviewed engine
  addition, never as cleverness in data.
- SSCC ranges are safe by construction: halt-not-wrap makes double issue
  impossible, and prefix-keyed sequences make a refresh a one-field config
  edit with a built-in audit trail. The cost is that a forgotten refresh
  stops Dachser bookings loudly rather than silently mis-numbering - the
  right failure direction.
- Fan-out manifests generalise the Manifest engine without touching the
  dispatch-confirmation trigger or the queue; a carrier's manifest can be a
  batch declaration or a per-consignment emission. Partial failure within a
  fan-out (order 30 of 50 fails) is a send-time concern settled in that
  chunk; SFTP uploads are overwrite-idempotent, so whole-manifest retry is
  the working assumption.
- Dachser's inverted flow (book = get-label, manifest = declare) is
  expressible with no special-casing: it is a book operation whose only job
  is a label, and a fan-out manifest operation - both ordinary definition
  data.

## Proving plan (chunks)

Each chunk is a bounded PR; 1-4 are reusable engine capabilities, 5
assembles Dachser:

1. `sftp_upload` transport backend (paramiko, fail-closed host-key pinning
   per decision 4) + a transport->uploader registry: the executor selects a
   backend by transport name, and a completeness test pins every
   schema-admitted upload transport to a backend - so an unbacked upload
   transport cannot enter the closed vocabulary at all, and any transport
   reaching the executor without one is refused there. (This is the ADR-0009
   follow-up; the closure is at build/execution time, not a publish-gate
   check - the closed, fully backed vocabulary leaves a publish gate nothing
   to catch.)
2. XML upload rendering: `content_type: "xml"`, `root_element`, the
   `@`-attribute convention, repeated elements via `each`.
3. SSCC: the `halt` allocator policy, prefix-keyed sequences, the GS1
   check-digit assembly routine, remaining-count + threshold log.
4. Per-consignment (fan-out) manifests in the manifest engine.
5. The Dachser definition (REST `/labels` book with `base64_pdf` label +
   SSCC; SFTP XML-EDI fan-out manifest) + `base64_pdf` label source.

Deferred: DigiDocs / commercial invoices (customs-paperwork subsystem,
Phase 6); the Voila transport-order path.
