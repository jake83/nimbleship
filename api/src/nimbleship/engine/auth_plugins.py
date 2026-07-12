"""Auth plugins: the extension point for carrier auth schemes the closed
vocabulary cannot say (OAuth token dances and friends, ADR 0009).

The executor calls the plugin named by a definition's `auth.scheme:
"plugin"` entry just before a rendered request is transmitted."""

from typing import Protocol

from nimbleship.engine.render import RenderedRequest


class AuthPlugin(Protocol):
    def apply(
        self, request: RenderedRequest, config: dict[str, object]
    ) -> RenderedRequest: ...


# Plugins register here, keyed by the name definitions reference in
# `auth.plugin`. The registry starts empty; each plugin module adds its
# entry at import time.
AUTH_PLUGINS: dict[str, AuthPlugin] = {}
