"""carrier number sequence wrap_after

A number range stores the wrap_after bound it was created with, so a later
allocation with a different bound is refused rather than silently wrapping a
counter early or issuing an out-of-range number. Nullable so rows created
before this column keep working; they backfill on their next allocation.

Revision ID: 35205bfdc77e
Revises: 27c1ab90344b
Create Date: 2026-07-15 01:30:51.369101

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "35205bfdc77e"
down_revision: str | Sequence[str] | None = "27c1ab90344b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "carrier_number_sequences",
        sa.Column("wrap_after", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("carrier_number_sequences", "wrap_after")
