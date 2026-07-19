"""The Legacy Interface's HTTP surface (ADR 0002): SOAP endpoints the WMS
posts to, gated by HTTP Basic Auth. Each endpoint translates the dialect onto
the domain's shape and serialises the reply back, with no business logic of its
own. Per ADR 0011 the create and allocate calls stage; only paperwork calls the
domain core (the same operations the JSON API uses)."""

import secrets
from collections.abc import Callable, Mapping
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from nimbleship.config import get_settings
from nimbleship.db import get_session
from nimbleship.http_client import get_http_client
from nimbleship.labels.store import LabelStore, get_label_store
from nimbleship.legacy import (
    allocation_service,
    consignment_service,
    manifest_service,
    soap,
)
from nimbleship.legacy.soap import SoapFault
from nimbleship.uploaders import FileUploader, get_carrier_uploaders

router = APIRouter(tags=["legacy"])

SessionDep = Annotated[Session, Depends(get_session)]
LabelStoreDep = Annotated[LabelStore, Depends(get_label_store)]
HttpClientDep = Annotated[httpx.Client, Depends(get_http_client)]
UploaderDep = Annotated[Mapping[str, FileUploader], Depends(get_carrier_uploaders)]

# A WMS consignment batch is small; cap the read so an oversized body is refused
# (defusedxml stops entity amplification, not raw size). The app-wide middleware
# bounds memory first - it buffers every body up to its higher global cap before any
# route runs - so this streaming read is the edge's tighter contract check, not the
# memory backstop.
_MAX_BODY_BYTES = 5 * 1024 * 1024


async def _raw_body(request: Request) -> bytes:
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


RawBody = Annotated[bytes, Depends(_raw_body)]

_basic = HTTPBasic(auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=401,
    detail="the Legacy Interface requires WMS credentials",
    headers={"WWW-Authenticate": "Basic"},
)


def require_wms(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> None:
    settings = get_settings()
    expected_user = settings.legacy_wms_username
    expected_password = settings.legacy_wms_password
    # Closed until configured: no credential means no WMS surface.
    if expected_user is None or expected_password is None or credentials is None:
        raise _UNAUTHORIZED
    # Both compared regardless of the first result, so the response time does not
    # reveal whether the username alone was right.
    user_ok = secrets.compare_digest(credentials.username, expected_user)
    password_ok = secrets.compare_digest(credentials.password, expected_password)
    if not (user_ok and password_ok):
        raise _UNAUTHORIZED


WmsAuth = Depends(require_wms)


@router.post("/ConsignmentService", dependencies=[WmsAuth])
def consignment_service_endpoint(
    body: RawBody,
    session: SessionDep,
    store: LabelStoreDep,
    http_client: HttpClientDep,
    uploaders: UploaderDep,
) -> Response:
    # ConsignmentService carries createPaperworkForConsignments, the one call
    # that runs the domain core, so this endpoint injects its dependencies.
    return _dispatch(
        session,
        lambda: consignment_service.handle(
            body, session, store, http_client, uploaders
        ),
    )


@router.post("/AllocationService", dependencies=[WmsAuth])
def allocation_service_endpoint(body: RawBody, session: SessionDep) -> Response:
    return _dispatch(session, lambda: allocation_service.handle(body, session))


@router.post("/ManifestService", dependencies=[WmsAuth])
def manifest_service_endpoint(body: RawBody, session: SessionDep) -> Response:
    # createManifest defers the carrier send to the queue, so it needs no
    # carrier-call dependencies - only the session.
    return _dispatch(session, lambda: manifest_service.handle(body, session))


def _dispatch(session: Session, run: Callable[[], bytes]) -> Response:
    try:
        return _reply(run())
    except SoapFault as error:
        # Discard any rows a partly-processed batch flushed, so the fault the WMS
        # sees means nothing was staged - the request-scoped commit would
        # otherwise persist the good items of a batch that failed on a later one.
        session.rollback()
        # SOAP 1.1's HTTP binding returns 500 for every fault, Client or Server.
        return _reply(soap.fault(str(error)), status_code=500)


def _reply(xml: bytes, status_code: int = 200) -> Response:
    return Response(
        content=xml, media_type="text/xml; charset=utf-8", status_code=status_code
    )
