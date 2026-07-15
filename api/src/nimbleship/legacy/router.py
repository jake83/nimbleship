"""The Legacy Interface's HTTP surface (ADR 0002): SOAP endpoints the WMS
posts to, gated by HTTP Basic Auth. Each endpoint parses the dialect, calls
the same domain operations as the JSON API, and serialises the reply back -
no business logic of its own."""

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from nimbleship.config import get_settings
from nimbleship.db import get_session
from nimbleship.legacy import consignment_service, soap
from nimbleship.legacy.soap import SoapFault

router = APIRouter(tags=["legacy"])

SessionDep = Annotated[Session, Depends(get_session)]

# A WMS consignment batch is small; cap the read so a hostile or runaway caller
# cannot exhaust memory with a giant body (defusedxml stops entity amplification,
# not raw size).
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
def consignment_service_endpoint(body: RawBody, session: SessionDep) -> Response:
    try:
        return _reply(consignment_service.handle(body, session))
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
