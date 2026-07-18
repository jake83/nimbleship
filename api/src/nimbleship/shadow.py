"""Shadow mode (ADR 0015): replay recorded incumbent WMS traffic through the real
legacy edge, side-effect-free, and diff NimbleShip's allocation against what the
incumbent did - flagging divergences for review, never cloning bug-for-bug.

The first slice diffs the allocation decision. A recording holds an order's real
create+allocate SOAP plus the incumbent's own outcome; replay drives those through
the edge in a rolled-back savepoint, so no staged row, booking, or label survives.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from nimbleship.domain.consignments import ConsignmentError
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


@dataclass(frozen=True)
class AllocationDiff:
    order_number: str
    incumbent: AllocationOutcome
    nimbleship: AllocationOutcome

    @property
    def matched(self) -> bool:
        return self.incumbent == self.nimbleship


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
    """Replay one recording's create+allocate through the real edge and diff
    NimbleShip's allocation against the incumbent's. Strictly side-effect-free: it
    runs inside a savepoint that is always rolled back, so no staged row survives.
    This is only safe because the replay stops at allocate_only - the booking path
    commits carrier traffic and minted ranges on separate sessions a savepoint
    rollback would not undo, so shadow must never reach it. store/http_client/
    uploaders are the edge's paperwork deps, inert here - shadow replays
    create+allocate, never the booking paperwork call."""
    savepoint = session.begin_nested()
    try:
        nimbleship = _replay(session, recording, store, http_client, uploaders)
    except (ConsignmentError, SoapFault) as error:
        # NimbleShip rejecting or faulting where the incumbent allocated is a
        # divergence to review, not a harness crash.
        nimbleship = AllocationOutcome(allocated=False, error=str(error))
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
