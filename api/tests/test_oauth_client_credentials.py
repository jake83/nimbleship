"""The oauth_client_credentials auth plugin: fetches a bearer token with a
form-encoded client-credentials grant, caches it per (token_url, client_id)
honouring expires_in with a safety margin, and injects the Authorization
header into a rendered request. All carrier contact is faked with
httpx.MockTransport - these tests never touch a network."""

from urllib.parse import parse_qs

import httpx
import pytest

from nimbleship.engine.plugins.oauth_client_credentials import (
    OAuthClientCredentialsAuth,
    OAuthTokenError,
)
from nimbleship.engine.render import RenderedRequest

TOKEN_URL = "https://apis.carrier.example/oauth/token"

CONFIG: dict[str, object] = {
    "token_url": TOKEN_URL,
    "client_id": "CLIENT-1",
    "client_secret": "SECRET-1",
}


def rendered_request() -> RenderedRequest:
    return RenderedRequest(
        step="ship",
        transport="http",
        method="POST",
        url="https://apis.carrier.example/ship/v1/shipments",
        query={},
        headers={"X-Existing": "kept"},
        content_type="json",
        body={"labelResponseOptions": "LABEL"},
    )


class FakeTokenServer:
    """A stand-in token endpoint counting fetches and capturing grants."""

    def __init__(
        self,
        token: str = "TOKEN-A",
        expires_in: int = 3600,
        status_code: int = 200,
    ) -> None:
        self.token = token
        self.expires_in = expires_in
        self.status_code = status_code
        self.fetches = 0
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.fetches += 1
        self.requests.append(request)
        if self.status_code != 200:
            return httpx.Response(self.status_code, json={"errors": ["nope"]})
        return httpx.Response(
            200,
            json={
                "access_token": self.token,
                "token_type": "bearer",
                "expires_in": self.expires_in,
            },
        )

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handle))


class ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_injects_a_bearer_token_and_keeps_existing_headers() -> None:
    server = FakeTokenServer(token="TOKEN-A")
    plugin = OAuthClientCredentialsAuth(http_client=server.client())

    authed = plugin.apply(rendered_request(), CONFIG)

    assert authed.headers["Authorization"] == "Bearer TOKEN-A"
    assert authed.headers["X-Existing"] == "kept"
    assert authed.body == {"labelResponseOptions": "LABEL"}


def test_the_original_request_is_left_untouched() -> None:
    server = FakeTokenServer()
    plugin = OAuthClientCredentialsAuth(http_client=server.client())
    request = rendered_request()

    plugin.apply(request, CONFIG)

    assert "Authorization" not in request.headers


def test_the_grant_is_form_encoded_client_credentials() -> None:
    server = FakeTokenServer()
    plugin = OAuthClientCredentialsAuth(http_client=server.client())

    plugin.apply(rendered_request(), CONFIG)

    [token_request] = server.requests
    assert str(token_request.url) == TOKEN_URL
    assert token_request.method == "POST"
    content_type = token_request.headers["Content-Type"]
    assert content_type == "application/x-www-form-urlencoded"
    assert parse_qs(token_request.content.decode()) == {
        "grant_type": ["client_credentials"],
        "client_id": ["CLIENT-1"],
        "client_secret": ["SECRET-1"],
    }


def test_two_requests_fetch_the_token_once() -> None:
    server = FakeTokenServer()
    plugin = OAuthClientCredentialsAuth(http_client=server.client())

    first = plugin.apply(rendered_request(), CONFIG)
    second = plugin.apply(rendered_request(), CONFIG)

    assert server.fetches == 1
    assert first.headers["Authorization"] == second.headers["Authorization"]


def test_the_token_is_refreshed_after_expiry() -> None:
    server = FakeTokenServer(expires_in=3600)
    clock = ManualClock()
    plugin = OAuthClientCredentialsAuth(http_client=server.client(), clock=clock)

    plugin.apply(rendered_request(), CONFIG)
    server.token = "TOKEN-B"
    clock.now += 3600

    refreshed = plugin.apply(rendered_request(), CONFIG)

    assert server.fetches == 2
    assert refreshed.headers["Authorization"] == "Bearer TOKEN-B"


def test_the_token_is_refreshed_within_the_safety_margin_of_expiry() -> None:
    """A token about to die mid-flight is as good as dead: refresh before
    the carrier's expires_in, not at it."""
    server = FakeTokenServer(expires_in=3600)
    clock = ManualClock()
    plugin = OAuthClientCredentialsAuth(http_client=server.client(), clock=clock)

    plugin.apply(rendered_request(), CONFIG)
    clock.now += 3600 - 30  # 30s left: inside any sensible safety margin

    plugin.apply(rendered_request(), CONFIG)

    assert server.fetches == 2


def test_a_token_well_before_expiry_is_not_refreshed() -> None:
    server = FakeTokenServer(expires_in=3600)
    clock = ManualClock()
    plugin = OAuthClientCredentialsAuth(http_client=server.client(), clock=clock)

    plugin.apply(rendered_request(), CONFIG)
    clock.now += 1800

    plugin.apply(rendered_request(), CONFIG)

    assert server.fetches == 1


def test_the_cache_is_keyed_by_token_url_and_client_id() -> None:
    server = FakeTokenServer()
    plugin = OAuthClientCredentialsAuth(http_client=server.client())
    other_client: dict[str, object] = {**CONFIG, "client_id": "CLIENT-2"}

    plugin.apply(rendered_request(), CONFIG)
    plugin.apply(rendered_request(), other_client)

    assert server.fetches == 2


def test_a_non_200_token_response_fails_loudly() -> None:
    server = FakeTokenServer(status_code=401)
    plugin = OAuthClientCredentialsAuth(http_client=server.client())

    with pytest.raises(OAuthTokenError, match="401"):
        plugin.apply(rendered_request(), CONFIG)


def test_a_token_response_without_an_access_token_fails_loudly() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"expires_in": 3600})

    client = httpx.Client(transport=httpx.MockTransport(handle))
    plugin = OAuthClientCredentialsAuth(http_client=client)

    with pytest.raises(OAuthTokenError, match="access_token"):
        plugin.apply(rendered_request(), CONFIG)


def test_missing_config_keys_fail_loudly_naming_the_key() -> None:
    server = FakeTokenServer()
    plugin = OAuthClientCredentialsAuth(http_client=server.client())
    config: dict[str, object] = {"token_url": TOKEN_URL, "client_id": "CLIENT-1"}

    with pytest.raises(OAuthTokenError, match="client_secret"):
        plugin.apply(rendered_request(), config)

    assert server.fetches == 0


def test_the_plugin_is_registered_under_its_definition_name() -> None:
    import nimbleship.engine.plugins  # noqa: F401  (registration side effect)
    from nimbleship.engine.auth_plugins import AUTH_PLUGINS

    plugin = AUTH_PLUGINS["oauth_client_credentials"]

    assert isinstance(plugin, OAuthClientCredentialsAuth)


def test_a_non_json_token_response_is_an_oauth_error_not_a_crash() -> None:
    # A non-JSON 200 (a proxy/maintenance page) must be an OAuthTokenError.
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="<html>maintenance</html>")
        )
    )
    plugin = OAuthClientCredentialsAuth(http_client=client)

    with pytest.raises(OAuthTokenError):
        plugin.apply(rendered_request(), CONFIG)


def test_oauth_token_error_is_an_auth_error() -> None:
    # The executor catches AuthError to route auth failures through
    # CarrierCallError; OAuthTokenError must be one so its failures are caught.
    from nimbleship.engine.auth_plugins import AuthError

    assert issubclass(OAuthTokenError, AuthError)
