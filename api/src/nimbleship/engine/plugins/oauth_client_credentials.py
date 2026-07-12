"""OAuth2 client-credentials auth: the `oauth_client_credentials` plugin.

Fetches a bearer token from the config-supplied `token_url` with a
form-encoded client-credentials grant (`client_id`, `client_secret` config
keys) and injects `Authorization: Bearer <token>` into the rendered
request. Tokens are cached in memory keyed by (token_url, client_id) and
treated as expired a safety margin before the endpoint's `expires_in`, so
a token never dies mid-flight.

The cache is per-process: every worker fetches and holds its own token.
Client-credentials endpoints permit this - each grant is independent and
issuing a new token does not revoke earlier ones. Concurrent first uses in
one process may fetch twice; both tokens are valid and the last write wins,
so no lock is held across the network call.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from nimbleship.engine.render import RenderedRequest

_SAFETY_MARGIN_SECONDS = 60.0
_TOKEN_FETCH_TIMEOUT_SECONDS = 10.0


class OAuthTokenError(Exception):
    """A bearer token could not be obtained: bad config, a non-200 token
    response, or a token payload missing its grant fields."""


@dataclass(frozen=True)
class _CachedToken:
    access_token: str
    expires_at: float  # on the plugin's clock, safety margin already applied


class OAuthClientCredentialsAuth:
    """AuthPlugin fetching and caching client-credentials bearer tokens.

    `http_client` is injectable so tests fake the token endpoint with
    httpx.MockTransport; when omitted, a short-lived client is opened per
    fetch (fetches are rare - one per token lifetime)."""

    def __init__(
        self,
        http_client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http_client = http_client
        self._clock = clock
        self._tokens: dict[tuple[str, str], _CachedToken] = {}

    def apply(
        self, request: RenderedRequest, config: dict[str, object]
    ) -> RenderedRequest:
        token = self._token(
            token_url=_required(config, "token_url"),
            client_id=_required(config, "client_id"),
            client_secret=_required(config, "client_secret"),
        )
        headers = {**request.headers, "Authorization": f"Bearer {token}"}
        return request.model_copy(update={"headers": headers})

    def _token(self, token_url: str, client_id: str, client_secret: str) -> str:
        key = (token_url, client_id)
        cached = self._tokens.get(key)
        if cached is not None and self._clock() < cached.expires_at:
            return cached.access_token
        fetched = self._fetch(token_url, client_id, client_secret)
        self._tokens[key] = fetched
        return fetched.access_token

    def _fetch(
        self, token_url: str, client_id: str, client_secret: str
    ) -> _CachedToken:
        grant = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if self._http_client is not None:
            response = self._http_client.post(token_url, data=grant)
        else:
            with httpx.Client(timeout=_TOKEN_FETCH_TIMEOUT_SECONDS) as client:
                response = client.post(token_url, data=grant)
        if response.status_code != 200:
            raise OAuthTokenError(
                f"token endpoint {token_url} answered {response.status_code}: "
                f"{response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise OAuthTokenError(f"token endpoint {token_url} answered non-object")
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise OAuthTokenError(
                f"token endpoint {token_url} answered without an access_token"
            )
        expires_in = payload.get("expires_in")
        if not isinstance(expires_in, int | float) or isinstance(expires_in, bool):
            raise OAuthTokenError(
                f"token endpoint {token_url} answered without a numeric expires_in"
            )
        return _CachedToken(
            access_token=access_token,
            expires_at=self._clock() + float(expires_in) - _SAFETY_MARGIN_SECONDS,
        )


def _required(config: dict[str, object], key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise OAuthTokenError(
            f"oauth_client_credentials needs config '{key}' as a non-empty string"
        )
    return value
