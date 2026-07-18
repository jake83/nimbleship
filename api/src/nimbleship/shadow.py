"""Shadow mode (ADR 0015): replay recorded incumbent WMS traffic through the real
legacy edge, side-effect-free, and diff NimbleShip's allocation against what the
incumbent did - flagging divergences for review, never cloning bug-for-bug.

A recording holds an order's real create+allocate SOAP plus the incumbent's own
outcome; replay drives it through the edge in a rolled-back savepoint, so nothing
survives. Two slices: `replay_allocation` diffs the allocation decision (stops at
allocate_only, before booking); `replay_paperwork` goes further for a local-render
order (dropout) that makes no carrier call - producing the label into an in-memory
store - and diffs the Parcels String and that a valid label was produced.
"""

import base64
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from nimbleship.domain.consignments import ConsignmentError
from nimbleship.domain.definitions import active_definition
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
    """NimbleShip's paperwork result for a recording: whether it produced a valid
    label, its Parcels String, or the error it raised."""

    label_produced: bool
    parcels_string: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PaperworkDiff:
    order_number: str
    incumbent_parcels_string: str | None
    nimbleship: PaperworkOutcome

    @property
    def matched(self) -> bool:
        return (
            self.nimbleship.error is None
            and self.nimbleship.label_produced
            and self.nimbleship.parcels_string == self.incumbent_parcels_string
        )


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("shadow paperwork replay must not call a carrier")


def replay_paperwork(session: Session, recording: GoldenRecording) -> PaperworkDiff:
    """Replay create+allocate+paperwork for a local-render order through the real
    edge and diff NimbleShip's Parcels String (and that it produced a label)
    against the incumbent's. Side-effect-free: an in-memory label store (no disk),
    no carrier call (local-render), all in a rolled-back savepoint (ADR 0015)."""
    store = _InMemoryLabelStore()
    savepoint = session.begin_nested()
    try:
        with httpx.Client(transport=httpx.MockTransport(_refuse)) as http_client:
            nimbleship = _replay_paperwork(session, recording, store, http_client)
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
        recording.order_number, recording.incumbent_parcels_string, nimbleship
    )


def _replay_paperwork(
    session: Session,
    recording: GoldenRecording,
    store: LabelStore,
    http_client: httpx.Client,
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
    external = _books_outside_savepoint(session, code)
    if external is not None:
        # The message states the structural fact (not local-render), not that
        # booking was reached: an unsupported label source would fault first (see
        # _books_outside_savepoint).
        return PaperworkOutcome(
            label_produced=False,
            error=f"carrier '{external}' is not a local-render carrier (its book "
            "operation declares an allocation mint or a carrier call); the "
            "paperwork slice replays local-render carriers only",
        )
    paperwork = paperwork_service.shadow_paperwork(
        session, code, store, http_client, uploaders
    )
    label = (
        base64.b64decode(paperwork.labels_base64) if paperwork.labels_base64 else b""
    )
    return PaperworkOutcome(
        label_produced=label.startswith(b"%PDF"),
        parcels_string=paperwork.parcels,
    )


def _books_outside_savepoint(session: Session, code: str) -> str | None:
    """The selected carrier's name if its book operation declares an allocation
    mint or a carrier call, else None. Both run on a separate committing session
    that the outer savepoint can't roll back (an SSCC burn survives), so such a
    carrier makes replay_paperwork's side-effect-free promise false and the
    paperwork slice refuses it rather than book (ADR 0015)."""
    selected = paperwork_service.shadow_allocate(session, code).selected
    if selected is None:
        return None
    definition = active_definition(session, selected.carrier)
    book = definition.operations.get("book") if definition is not None else None
    if book is not None and (book.allocate or book.steps):
        return selected.carrier
    return None
