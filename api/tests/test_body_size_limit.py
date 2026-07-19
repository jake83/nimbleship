"""Tests for the app-wide body-byte cap (nimbleship.middleware)."""

from collections.abc import Iterator

from fastapi.testclient import TestClient

from nimbleship.legacy.router import _MAX_BODY_BYTES as LEGACY_MAX_BODY_BYTES
from nimbleship.middleware import MAX_BODY_BYTES


def test_the_global_cap_sits_above_every_per_edge_ceiling() -> None:
    # The legacy WMS edge deliberately accepts batches up to its own cap; a global
    # backstop below it would silently shrink that contract.
    assert MAX_BODY_BYTES > LEGACY_MAX_BODY_BYTES


def test_an_over_declared_body_is_a_clean_413(client: TestClient) -> None:
    oversized = b"x" * (MAX_BODY_BYTES + 1)
    response = client.post(
        "/api/carrier-builder/check",
        content=oversized,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert "too large" in response.text


def test_a_lying_declaration_cannot_smuggle_an_over_cap_body(
    client: TestClient,
) -> None:
    # Content-Length is sender-controlled: a real server rejects a mismatch at its
    # framing layer, but the cap must hold without trusting that. httpx honours a
    # caller-supplied Content-Length over the computed one, so the lie is expressible
    # black-box - on the old declared-length fast path this body smuggled through.
    response = client.post(
        "/api/carrier-builder/check",
        content=b"x" * (MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json", "Content-Length": "10"},
    )
    assert response.status_code == 413
    assert "too large" in response.text


def test_an_undeclared_chunked_body_over_the_cap_is_a_clean_413(
    client: TestClient,
) -> None:
    # A generator body sends chunked with no Content-Length, so there is no
    # declaration to pre-check - only the buffered count can trip the cap.
    def chunks() -> Iterator[bytes]:
        sent = 0
        while sent <= MAX_BODY_BYTES:
            yield b"x" * 65536
            sent += 65536

    response = client.post(
        "/api/carrier-builder/check",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert "too large" in response.text


def test_an_undeclared_body_under_the_cap_replays_to_the_app(
    client: TestClient,
) -> None:
    # The buffered body must reach the route intact, not truncated or dropped.
    payload = b'{"definition": {"carrier": "acme", "name": "Acme"}}'

    def chunks() -> Iterator[bytes]:
        yield payload

    response = client.post(
        "/api/carrier-builder/check",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["valid"] is False  # parsed for real: incomplete definition


def test_a_large_but_legal_body_passes(client: TestClient) -> None:
    # The cap is a backstop, not a squeeze on real payloads: ~3 MB must go through.
    big_packet = "x" * (3 * 1024 * 1024)
    raw = ('{"definition": {"carrier": "' + big_packet + '"}}').encode()
    assert len(raw) > 2 * 1024 * 1024
    big = client.post(
        "/api/carrier-builder/check",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert big.status_code == 200


def test_a_get_request_is_untouched(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok"}
