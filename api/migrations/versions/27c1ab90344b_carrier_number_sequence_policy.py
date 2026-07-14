"""carrier number sequence policy

A number range stores the exhaustion policy (wrap or halt) it was created
with, so an exhausted halt range - one that must never reissue a live code,
like an SSCC - cannot be cycled by a later wrap allocation. Nullable so rows
created before this column keep working; they backfill on their next
allocation.

Revision ID: 27c1ab90344b
Revises: c41f7a9d2e58
Create Date: 2026-07-14 11:19:32.211949

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "27c1ab90344b"
down_revision: str | Sequence[str] | None = "c41f7a9d2e58"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "carrier_number_sequences",
        sa.Column("policy", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("carrier_number_sequences", "policy")
