from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from nimbleship import models  # noqa: F401  (registers all tables on Base)
from nimbleship.config import get_settings
from nimbleship.db import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The application's settings supply the database URL unless the caller
# already set one explicitly (tests and tooling pass their own URL via
# Config; overriding it here would silently migrate the wrong database -
# which is exactly what happened on PR #12's first CI run).
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def include_name(
    name: str | None, type_: str, parent_names: dict[str, str | None]
) -> bool:
    # The job queue's tables are Procrastinate's, installed by a migration
    # (ADR 0004) and absent from Base.metadata. Without this filter,
    # autogenerate sees them in the database, finds no model, and emits
    # drop_table for them - a boot-time `alembic upgrade head` would then
    # destroy the queue schema. Keep them invisible to autogenerate.
    return not (type_ == "table" and (name or "").startswith("procrastinate_"))


def run_migrations_offline() -> None:
    """Emit migration SQL without a live connection."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
