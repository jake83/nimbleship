"""The auth-scheme extension point (ADR 0009): a Carrier Definition with
plugin auth names an entry in this registry, and the executor applies it to
each rendered http request at execution time - never at render time, so
tokens and signatures stay out of renders and the Golden Replay corpus."""

from typing import Protocol

from nimbleship.engine.render import RenderedRequest


class AuthPlugin(Protocol):
    def apply(
        self, request: RenderedRequest, config: dict[str, object]
    ) -> RenderedRequest: ...


AUTH_PLUGINS: dict[str, AuthPlugin] = {}
