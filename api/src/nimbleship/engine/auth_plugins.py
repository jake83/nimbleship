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

    # The config keys the plugin reads straight from config at execution, not
    # via a config.* source in the definition. Declaring them lets the publish
    # completeness gate require them like any other config key.
    def required_config_keys(self) -> frozenset[str]: ...


# Plugins register here, keyed by the name definitions reference in
# `auth.plugin`. The registry starts empty; each plugin module adds its
# entry at import time.
AUTH_PLUGINS: dict[str, AuthPlugin] = {}


def auth_plugin_config_keys(name: str) -> frozenset[str]:
    # Empty for an unregistered name: an unknown plugin's config needs are
    # unknowable here, and authoring does not yet require the plugin to exist.
    import nimbleship.engine.plugins  # noqa: F401  (fill the registry)

    plugin = AUTH_PLUGINS.get(name)
    return plugin.required_config_keys() if plugin is not None else frozenset()
