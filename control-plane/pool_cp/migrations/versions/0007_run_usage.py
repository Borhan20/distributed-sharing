"""run usage: what each attempt used, as the device measured it (docs/design/pool-decides.md §4.2)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-06 12:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    op.drop_column("attempts", "usage")
