"""The outbound HTTP client carrier calls execute through, provided as a
FastAPI dependency so tests substitute an httpx.MockTransport client -
the test suite never touches a real network."""

from collections.abc import Iterator

import httpx

CARRIER_CALL_TIMEOUT_SECONDS = 30.0


def carrier_http_client() -> httpx.Client:
    """A client configured for carrier calls; callers own its lifetime.
    Queue jobs open one per job - they run outside request scope."""
    return httpx.Client(timeout=CARRIER_CALL_TIMEOUT_SECONDS)


def get_http_client() -> Iterator[httpx.Client]:
    with carrier_http_client() as client:
        yield client
