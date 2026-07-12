"""The baseline migration must produce exactly the schema the models
declare - otherwise Alembic and create_all drift apart silently."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from nimbleship.db import Base

API_ROOT = Path(__file__).parent.parent


def _upgraded_engine_tables(database_url: str) -> set[str]:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_upgrade_head_creates_every_model_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("NIMBLESHIP_DATABASE_URL", url)

    tables = _upgraded_engine_tables(url)

    expected = set(Base.metadata.tables)
    assert expected <= tables
    assert "alembic_version" in tables
