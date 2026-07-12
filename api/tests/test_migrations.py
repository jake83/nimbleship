"""The baseline migration must produce exactly the schema the models
declare - otherwise Alembic and create_all drift apart silently."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from nimbleship.db import Base

API_ROOT = Path(__file__).parent.parent


def _upgraded_schema(database_url: str) -> dict[str, set[str]]:
    config = Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
        }
    finally:
        engine.dispose()


def test_upgrade_head_creates_every_model_table_and_column(tmp_path: Path) -> None:
    # Deliberately no NIMBLESHIP_DATABASE_URL in the environment: env.py must
    # honour the URL set on the Config, not silently migrate the settings
    # default (the PR #12 CI failure).
    url = f"sqlite:///{tmp_path / 'migrated.db'}"

    schema = _upgraded_schema(url)

    assert "alembic_version" in schema
    for table in Base.metadata.tables.values():
        model_columns = {column.name for column in table.columns}
        assert model_columns == schema[table.name], table.name
