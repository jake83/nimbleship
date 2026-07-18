"""consignment max girth

Revision ID: 3b8e5bb39b48
Revises: 94cc67866caa
Create Date: 2026-07-18 01:01:35.236146

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3b8e5bb39b48"
down_revision: str | Sequence[str] | None = "94cc67866caa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "consignments",
        sa.Column("max_girth_cm", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("consignments", "max_girth_cm")
