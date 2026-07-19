"""The app-wide request-body byte ceiling: per-field caps bound shapes, not bytes, so
an oversized blob is refused here, once, before any parsing."""

import asyncio

import pytest
from fastapi.testclient import TestClient

from nimbleship.middleware import (
    MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
    BodyTooLarge,
    Message,
    Scope,
)


def test_an_over_declared_body_is_a_clean_413(client: TestClient) -> None:
    oversized = "x" * (MAX_BODY_BYTES + 1)
    response = client.post(
        "/api/carrier-builder/check",
        content=oversized.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert "too large" in response.text


def test_a_normal_request_passes_untouched(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
    # A realistic large-but-legal payload (the packet cap's worst case) fits.
    response = client.post(
        "/api/carrier-builder/check", json={"definition": {"carrier": "acme"}}
    )
    assert response.status_code == 200


def test_an_undeclared_body_is_cut_off_at_the_cap() -> None:
    # Chunked bodies carry no Content-Length; the counting receive-wrapper stops them
    # at the cap rather than letting the app buffer without bound.
    middleware = BodySizeLimitMiddleware(_reads_whole_body, max_bytes=10)
    scope: Scope = {"type": "http", "method": "POST", "headers": []}

    chunks = [b"12345", b"67890", b"overflow"]

    async def receive() -> Message:
        body = chunks.pop(0)
        return {"type": "http.request", "body": body, "more_body": bool(chunks)}

    async def send(message: Message) -> None:  # pragma: no cover - not reached
        pass

    with pytest.raises(BodyTooLarge):
        asyncio.run(middleware(scope, receive, send))


async def _reads_whole_body(scope: Scope, receive, send) -> None:  # type: ignore[no-untyped-def]
    while True:
        message = await receive()
        if not message.get("more_body"):
            break
