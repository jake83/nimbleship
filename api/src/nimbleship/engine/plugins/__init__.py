"""Named engine plugins: the bounded escape hatch for what the declarative
vocabulary cannot say (ADR 0005, ADR 0009). Importing this package fills
the extension-point registries with every plugin a definition may name."""

from nimbleship.engine.auth_plugins import AUTH_PLUGINS
from nimbleship.engine.plugins.oauth_client_credentials import (
    OAuthClientCredentialsAuth,
)

AUTH_PLUGINS["oauth_client_credentials"] = OAuthClientCredentialsAuth()
