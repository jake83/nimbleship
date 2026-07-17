from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NIMBLESHIP_")

    database_url: str = "sqlite:///./nimbleship.db"
    # Test-environment capabilities (force allocation). Server-enforced:
    # endpoints return 403 when disabled; production deployments never
    # enable it.
    testing_tools_enabled: bool = False
    labels_dir: Path = Path("./labels")
    # HTTP Basic Auth for the WMS-facing Legacy Interface. Unset by default, so
    # the edge rejects every request until an install configures a credential -
    # it is never open by omission.
    legacy_wms_username: str | None = None
    legacy_wms_password: str | None = None
    # Shared secret for the Voila tracking webhook. Unset by default, so the
    # webhook rejects every post until an install configures it.
    voila_webhook_secret: str | None = None


def get_settings() -> Settings:
    return Settings()
