"""Shadow mode (ADR 0015): replay recorded incumbent WMS traffic through the real
legacy edge, side-effect-free, and diff NimbleShip's allocation against what the
incumbent did - flagging divergences for review, never cloning bug-for-bug.

A recording holds an order's real create+allocate SOAP plus the incumbent's own
outcome; replay drives it through the edge in a rolled-back savepoint, so nothing
survives. Two slices: `replay_allocation` diffs the allocation decision (stops at
allocate_only, before booking); `replay_paperwork` goes further and diffs the
label, Parcels String, and tracking reference. A booking carrier's real book step
runs against the recorded carrier response, with traffic sent to an in-memory sink
so nothing escapes the savepoint; SSCC-minting carriers are not yet replayed.
"""

import base64
import binascii
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from nimbleship.domain.consignments import BookingSideEffects, ConsignmentError
from nimbleship.domain.definitions import active_definition
from nimbleship.engine.execute import StepRecord
from nimbleship.labels.store import LabelStore
from nimbleship.legacy import allocation_service, consignment_service, paperwork_service
from nimbleship.legacy.soap import SoapFault
from nimbleship.models import LegacyConsignmentStaging
from nimbleship.uploaders import FileUploader


@dataclass(frozen=True)
class AllocationOutcome:
    """One side of an allocation diff: the chosen carrier/service, unallocated, or
    an error one system raised where the other produced a result."""

    allocated: bool
    carrier: str | None = None
    service: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class CarrierBookResponse:
    """The carrier's own book response recorded from real traffic; replay feeds it
    back through the real book step via a mock transport, so NimbleShip's own
    response parsing is diffed, not stubbed (ADR 0015). One response covers a
    single-step book operation (all carriers on the ladder so far)."""

    status: int
    body: str


@dataclass(frozen=True)
class GoldenRecording:
    """An order's real incumbent traffic plus the incumbent's own allocation
    outcome. The create/allocate SOAP replays through the edge; `incumbent_code` is
    the server-minted code the allocate SOAP references, mapped to NimbleShip's own
    code on replay; `incumbent` is the golden answer to diff against (ADR 0015)."""

    order_number: str
    create_consignments: bytes
    incumbent_code: str
    allocate_consignments: bytes
    incumbent: AllocationOutcome
    # The incumbent's Parcels String, for the paperwork slice; None for a
    # recording diffed only at allocation.
    incumbent_parcels_string: str | None = None
    # The incumbent's carrier-minted tracking reference; None for a local-render
    # order (no carrier call) or an allocation-only recording.
    incumbent_tracking_reference: str | None = None
    # The carrier's recorded book response, for a booking-carrier paperwork diff;
    # None for a local-render order that never calls a carrier.
    carrier_book_response: CarrierBookResponse | None = None
    # The incumbent's final label, base64. Set only for a carrier whose label is
    # byte-comparable (base64_pdf: both sides decode the same carrier PDF); a
    # local-render label differs per renderer, so leave it None and the diff falls
    # back to checking a valid label was produced.
    incumbent_label_base64: str | None = None


@dataclass(frozen=True)
class AllocationDiff:
    order_number: str
    incumbent: AllocationOutcome
    nimbleship: AllocationOutcome

    @property
    def matched(self) -> bool:
        # Only the decision dimensions are diffed (ADR 0015); error text is
        # diagnostic (WMS-native vs ours, always differing), never compared. A
        # NimbleShip fault is never a clean match - it is a gap worth surfacing,
        # even against a declining incumbent.
        if self.nimbleship.error is not None:
            return False
        return (
            self.incumbent.allocated,
            self.incumbent.carrier,
            self.incumbent.service,
        ) == (
            self.nimbleship.allocated,
            self.nimbleship.carrier,
            self.nimbleship.service,
        )


@dataclass(frozen=True)
class ShadowReport:
    diffs: tuple[AllocationDiff, ...]

    @property
    def matched(self) -> int:
        return sum(1 for diff in self.diffs if diff.matched)

    @property
    def divergences(self) -> tuple[AllocationDiff, ...]:
        return tuple(diff for diff in self.diffs if not diff.matched)


def replay_allocation(
    session: Session,
    recording: GoldenRecording,
    store: LabelStore,
    http_client: httpx.Client,
    uploaders: Mapping[str, FileUploader],
) -> AllocationDiff:
    """Replay one recording's create+allocate through the real edge, diff its
    allocation against the incumbent's, side-effect-free via a rolled-back savepoint
    - safe only because replay stops at allocate_only; the booking path commits
    carrier traffic on separate sessions a rollback can't undo. store/http_client/
    uploaders are inert here (the edge's paperwork deps, never reached)."""
    savepoint = session.begin_nested()
    try:
        nimbleship = _replay(session, recording, store, http_client, uploaders)
    except (ConsignmentError, SoapFault) as error:
        # NimbleShip rejecting or faulting where the incumbent allocated is a
        # divergence to review, not a harness crash.
        nimbleship = AllocationOutcome(allocated=False, error=str(error))
    except NoResultFound:
        # The recording's order_number did not stage - a capture-quality glitch
        # (its order_number disagrees with its own create payload). Isolate it as
        # a bad recording so replay_all still processes the rest of the batch.
        nimbleship = AllocationOutcome(
            allocated=False,
            error=f"no staged consignment for order '{recording.order_number}': "
            "the recording's order_number may not match its create payload",
        )
    finally:
        savepoint.rollback()
    return AllocationDiff(recording.order_number, recording.incumbent, nimbleship)


def replay_all(
    session: Session,
    recordings: list[GoldenRecording],
    store: LabelStore,
    http_client: httpx.Client,
    uploaders: Mapping[str, FileUploader],
) -> ShadowReport:
    return ShadowReport(
        tuple(
            replay_allocation(session, recording, store, http_client, uploaders)
            for recording in recordings
        )
    )


def _replay(
    session: Session,
    recording: GoldenRecording,
    store: LabelStore,
    http_client: httpx.Client,
    uploaders: Mapping[str, FileUploader],
) -> AllocationOutcome:
    consignment_service.handle(
        recording.create_consignments, session, store, http_client, uploaders
    )
    code = _staged_code(session, recording.order_number)
    # The recorded allocate names the incumbent's server-minted code; NimbleShip
    # mints its own, so map it before replaying the call verbatim (ADR 0015).
    allocate = recording.allocate_consignments.replace(
        recording.incumbent_code.encode(), code.encode()
    )
    allocation_service.handle(allocate, session)
    result = paperwork_service.shadow_allocate(session, code)
    selected = result.selected
    return AllocationOutcome(
        allocated=selected is not None,
        carrier=selected.carrier if selected is not None else None,
        service=selected.code if selected is not None else None,
    )


def _staged_code(session: Session, order_number: str) -> str:
    row = session.execute(
        select(LegacyConsignmentStaging).where(
            LegacyConsignmentStaging.order_number == order_number
        )
    ).scalar_one()
    code = row.consignment_code
    assert code is not None  # createConsignments mints it
    return code


class _InMemoryLabelStore(LabelStore):
    """A LabelStore that keeps labels in memory: a shadow replay produces a real
    label but writes nothing to disk (ADR 0015)."""

    def __init__(self) -> None:
        self._labels: dict[str, bytes] = {}

    def save(self, order_number: str, pdf: bytes) -> None:
        self._labels[order_number] = pdf

    def load(self, order_number: str) -> bytes | None:
        return self._labels.get(order_number)


@dataclass(frozen=True)
class PaperworkOutcome:
    """NimbleShip's paperwork result for a recording: its label bytes (and whether
    that is a valid PDF), its Parcels String, its carrier tracking reference, or the
    error it raised. Each is a separate output the WMS consumes on its own."""

    label_produced: bool
    label: bytes | None = None
    parcels_string: str | None = None
    tracking_reference: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PaperworkDiff:
    order_number: str
    incumbent_parcels_string: str | None
    incumbent_tracking_reference: str | None
    nimbleship: PaperworkOutcome
    # The incumbent's label bytes when byte-comparable (base64_pdf); None means the
    # label diff falls back to "a valid label was produced" (local render).
    incumbent_label: bytes | None = None

    @property
    def matched(self) -> bool:
        if self.nimbleship.error is not None:
            return False
        if self.incumbent_label is not None:
            # base64_pdf: both sides decode the same carrier PDF, so a byte match is
            # meaningful and catches a wrong extract path or a mis-decode.
            label_ok = self.nimbleship.label == self.incumbent_label
        else:
            # local render: the renderers differ, so only that a valid label exists.
            label_ok = self.nimbleship.label_produced
        return (
            label_ok
            and self.nimbleship.parcels_string == self.incumbent_parcels_string
            and self.nimbleship.tracking_reference == self.incumbent_tracking_reference
        )


def _savepoint_side_effects() -> BookingSideEffects:
    """Shadow's booking side effects: discard carrier traffic and do not commit a
    failure, so a booking replay - whether the book step succeeds or its recorded
    response fails - stays inside the rolled-back savepoint instead of leaking past
    it (ADR 0015). The raised error still becomes a divergence."""

    def discard_traffic(carrier: str, order_number: str, step: StepRecord) -> None:
        pass

    def keep_in_savepoint() -> None:
        pass

    return BookingSideEffects(
        record_traffic=discard_traffic, persist_failure=keep_in_savepoint
    )


def _carrier_responder(
    recording: GoldenRecording,
) -> Callable[[httpx.Request], httpx.Response]:
    """A mock transport that answers the book call with the carrier's recorded
    response, so NimbleShip's real book step runs without a live call. A
    local-render order never reaches it; a booking order without a recorded
    response is a bad recording, faulted loudly rather than silently mis-diffed."""

    def handler(request: httpx.Request) -> httpx.Response:
        response = recording.carrier_book_response
        if response is None:
            raise AssertionError(
                "shadow paperwork replay reached a carrier call with no recorded "
                f"response for order '{recording.order_number}'"
            )
        return httpx.Response(response.status, text=response.body)

    return handler


def replay_paperwork(session: Session, recording: GoldenRecording) -> PaperworkDiff:
    """Replay create+allocate+paperwork through the real edge and diff NimbleShip's
    label, Parcels String, and tracking reference against the incumbent's. A booking
    carrier's real book step runs against the recorded response; side-effect-free
    via an in-memory label store, an in-memory traffic sink, and a rolled-back
    savepoint (ADR 0015). SSCC-minting carriers are not yet replayed."""
    try:
        incumbent_label = (
            base64.b64decode(recording.incumbent_label_base64)
            if recording.incumbent_label_base64
            else None
        )
    except binascii.Error:
        # A corrupt captured label is a bad recording, not a NimbleShip fault (cf.
        # the NoResultFound handling): surface it as a divergence, never a crash.
        return PaperworkDiff(
            recording.order_number,
            recording.incumbent_parcels_string,
            recording.incumbent_tracking_reference,
            PaperworkOutcome(
                label_produced=False,
                error="the recording's incumbent label is not valid base64",
            ),
            None,
        )
    store = _InMemoryLabelStore()
    savepoint = session.begin_nested()
    try:
        transport = httpx.MockTransport(_carrier_responder(recording))
        with httpx.Client(transport=transport) as http_client:
            nimbleship = _replay_paperwork(
                session, recording, store, http_client, _savepoint_side_effects()
            )
    except (ConsignmentError, SoapFault) as error:
        nimbleship = PaperworkOutcome(label_produced=False, error=str(error))
    except NoResultFound:
        nimbleship = PaperworkOutcome(
            label_produced=False,
            error=f"no staged consignment for order '{recording.order_number}': "
            "the recording's order_number may not match its create payload",
        )
    finally:
        savepoint.rollback()
    return PaperworkDiff(
        recording.order_number,
        recording.incumbent_parcels_string,
        recording.incumbent_tracking_reference,
        nimbleship,
        incumbent_label,
    )


def _replay_paperwork(
    session: Session,
    recording: GoldenRecording,
    store: LabelStore,
    http_client: httpx.Client,
    side_effects: BookingSideEffects,
) -> PaperworkOutcome:
    uploaders: Mapping[str, FileUploader] = {}
    consignment_service.handle(
        recording.create_consignments, session, store, http_client, uploaders
    )
    code = _staged_code(session, recording.order_number)
    allocate = recording.allocate_consignments.replace(
        recording.incumbent_code.encode(), code.encode()
    )
    allocation_service.handle(allocate, session)
    external = _mints_outside_savepoint(session, code)
    if external is not None:
        # See _mints_outside_savepoint: the SSCC mint commits on a separate session
        # the rolled-back savepoint can't undo, and the slice does not yet feed
        # recorded SSCCs, so refuse rather than leak.
        return PaperworkOutcome(
            label_produced=False,
            error=f"carrier '{external}' mints client-side allocations (SSCC), "
            "not yet replayed by the paperwork slice",
        )
    paperwork = paperwork_service.shadow_paperwork(
        session, code, store, http_client, uploaders, side_effects
    )
    label = (
        base64.b64decode(paperwork.labels_base64) if paperwork.labels_base64 else b""
    )
    return PaperworkOutcome(
        label_produced=label.startswith(b"%PDF"),
        label=label or None,
        parcels_string=paperwork.parcels,
        tracking_reference=paperwork.tracking_reference,
    )


def _mints_outside_savepoint(session: Session, code: str) -> str | None:
    """The selected carrier's name if its book operation mints client-side
    allocations (SSCC), else None. Minting commits on a separate session the outer
    savepoint can't roll back, and the paperwork slice does not yet feed recorded
    SSCCs, so it refuses such a carrier rather than leak (ADR 0015). A carrier call
    by itself is fine - its traffic goes to shadow's in-memory sink, inside the
    savepoint - so http-book carriers replay."""
    selected = paperwork_service.shadow_allocate(session, code).selected
    if selected is None:
        return None
    definition = active_definition(session, selected.carrier)
    book = definition.operations.get("book") if definition is not None else None
    if book is not None and book.allocate:
        return selected.carrier
    return None
