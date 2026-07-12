from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NIMBLESHIP_")

    database_url: str = "sqlite:///./nimbleship.db"
    labels_dir: Path = Path("./labels")


def get_settings() -> Settings:
    return Settings()
