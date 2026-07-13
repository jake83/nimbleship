"""manifests and the job queue

Manifests (one per carrier and warehouse per dispatch confirmation) and
Procrastinate's schema: the job queue lives in the same database as the
domain writes that enqueue into it (ADR 0004), so the migration chain owns
both. Procrastinate's objects exist only on Postgres - SQLite serves the
unit suite, which never runs the queue.

A future Procrastinate upgrade ships its own migration scripts
(procrastinate.schema.SchemaManager.get_migrations_path); apply them in a
new revision here rather than re-running the bundled schema.

Revision ID: c41f7a9d2e58
Revises: 378dd6182211
Create Date: 2026-07-13 10:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from procrastinate.schema import SchemaManager

# revision identifiers, used by Alembic.
revision: str = "c41f7a9d2e58"
down_revision: str | Sequence[str] | None = "378dd6182211"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Everything Procrastinate creates is prefixed procrastinate_; the schema
# ships no teardown script, so downgrade drops by catalogue. Tables go
# first (their triggers ride along), then functions, then standalone types.
DROP_PROCRASTINATE = """
DO $$
DECLARE
    name text;
    args text;
BEGIN
    FOR name IN
        SELECT tablename FROM pg_tables
        WHERE schemaname = current_schema() AND tablename LIKE 'procrastinate_%'
    LOOP
        EXECUTE format('DROP TABLE IF EXISTS %I CASCADE', name);
    END LOOP;
    FOR name, args IN
        SELECT p.proname, pg_get_function_identity_arguments(p.oid)
        FROM pg_proc p JOIN pg_namespace n ON p.pronamespace = n.oid
        WHERE n.nspname = current_schema() AND p.proname LIKE 'procrastinate_%'
    LOOP
        EXECUTE format('DROP FUNCTION IF EXISTS %I(%s) CASCADE', name, args);
    END LOOP;
    FOR name IN
        SELECT t.typname FROM pg_type t JOIN pg_namespace n ON t.typnamespace = n.oid
        WHERE n.nspname = current_schema()
          AND t.typname LIKE 'procrastinate_%' AND t.typtype IN ('e', 'c')
    LOOP
        EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', name);
    END LOOP;
END
$$;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "manifests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("carrier", sa.String(length=64), nullable=False),
        sa.Column("warehouse", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_manifests_carrier"), "manifests", ["carrier"], unique=False
    )
    op.create_table(
        "manifest_consignments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("manifest_id", sa.Integer(), nullable=False),
        sa.Column("consignment_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["consignment_id"], ["consignments.id"]),
        sa.ForeignKeyConstraint(["manifest_id"], ["manifests.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manifest_id", "consignment_id"),
    )
    op.create_index(
        op.f("ix_manifest_consignments_manifest_id"),
        "manifest_consignments",
        ["manifest_id"],
        unique=False,
    )
    if op.get_bind().dialect.name == "postgresql":
        _execute_script(SchemaManager.get_schema())


def _execute_script(sql: str) -> None:
    """Run a multi-statement script on the raw psycopg connection: both
    scripts carry literal % signs (plpgsql format strings) that every
    parameterising execution path would reject as placeholders."""
    connection = op.get_bind().connection.driver_connection
    assert connection is not None
    connection.execute(sql)  # type: ignore[union-attr]


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "postgresql":
        _execute_script(DROP_PROCRASTINATE)
    op.drop_index(
        op.f("ix_manifest_consignments_manifest_id"),
        table_name="manifest_consignments",
    )
    op.drop_table("manifest_consignments")
    op.drop_index(op.f("ix_manifests_carrier"), table_name="manifests")
    op.drop_table("manifests")
