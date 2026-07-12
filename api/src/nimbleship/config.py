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


def get_settings() -> Settings:
    return Settings()
