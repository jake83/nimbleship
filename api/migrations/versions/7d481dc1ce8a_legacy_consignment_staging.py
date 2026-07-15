"""legacy consignment staging

The Legacy Interface holds inbound WMS data between its stateful lifecycle
calls here (ADR 0011): create and allocate accumulate, paperwork consumes.
Ephemeral - never the system of record. The consignment code is nullable
because it is derived from the autoincrement id and set immediately after
insert, within the minting transaction.

Revision ID: 7d481dc1ce8a
Revises: 35205bfdc77e
Create Date: 2026-07-15 23:58:47.899766

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7d481dc1ce8a"
down_revision: str | Sequence[str] | None = "35205bfdc77e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "legacy_consignment_staging",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("consignment_code", sa.String(length=32), nullable=True),
        sa.Column("order_number", sa.String(length=64), nullable=True),
        sa.Column("created_data", sa.JSON(), nullable=True),
        sa.Column("allocation_data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_legacy_consignment_staging_consignment_code"),
        "legacy_consignment_staging",
        ["consignment_code"],
        unique=True,
    )
    op.create_index(
        op.f("ix_legacy_consignment_staging_order_number"),
        "legacy_consignment_staging",
        ["order_number"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_legacy_consignment_staging_order_number"),
        table_name="legacy_consignment_staging",
    )
    op.drop_index(
        op.f("ix_legacy_consignment_staging_consignment_code"),
        table_name="legacy_consignment_staging",
    )
    op.drop_table("legacy_consignment_staging")
