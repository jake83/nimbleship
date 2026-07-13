"""The outbound HTTP client carrier calls execute through, provided as a
FastAPI dependency so tests substitute an httpx.MockTransport client -
the test suite never touches a real network."""

from collections.abc import Iterator

import httpx

CARRIER_CALL_TIMEOUT_SECONDS = 30.0


def get_http_client() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=CARRIER_CALL_TIMEOUT_SECONDS) as client:
        yield client
