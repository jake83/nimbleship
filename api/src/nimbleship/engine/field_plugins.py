"""Computed-field plugins: one of the four named-plugin extension points
(ADR 0009). A mapping entry may name a plugin instead of a source fact or
constant; the renderer calls the registered plugin with the facts and maps
the computed value to the entry's target.

The render path stays pure: a plugin computes from the facts it is handed
and nothing else. Anything stateful - allocating a number from a range,
minting a token - happens BEFORE render, in the dispatch integration, and
reaches the plugin as an injected fact (by convention under the
`allocated` root; see nimbleship.engine.plugins.number_range).

Plugin names are part of the definition vocabulary: a definition naming an
unregistered plugin fails at authoring time, never at booking time.
"""

from typing import Protocol


class FieldPlugin(Protocol):
    def compute(self, facts: dict[str, object]) -> object: ...


FIELD_PLUGINS: dict[str, FieldPlugin] = {}


def register(name: str, plugin: FieldPlugin) -> None:
    if name in FIELD_PLUGINS:
        raise ValueError(f"field plugin '{name}' is already registered")
    FIELD_PLUGINS[name] = plugin


def _ensure_builtins_loaded() -> None:
    # Built-in plugins register themselves on import; importing lazily here
    # (rather than at module top) keeps the registry importable from the
    # schema module without a cycle through the plugins' own imports.
    import nimbleship.engine.plugins  # noqa: F401


def field_plugin(name: str) -> FieldPlugin:
    _ensure_builtins_loaded()
    if name not in FIELD_PLUGINS:
        raise ValueError(f"unknown field plugin '{name}'")
    return FIELD_PLUGINS[name]


def field_plugin_names() -> set[str]:
    _ensure_builtins_loaded()
    return set(FIELD_PLUGINS)
