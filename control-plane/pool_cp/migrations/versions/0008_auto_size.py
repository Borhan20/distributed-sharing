"""auto size: pool chooses sizes that were left out (docs/design/pool-decides.md §4.3)

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-06 15:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("groups", sa.Column("default_time_limit_s", sa.Integer(), server_default="14400", nullable=False))
    op.add_column("jobs", sa.Column("max_runtime_s", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("sizing", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("attempts", sa.Column("resources", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("usage_ledger", sa.Column("pool_paid", sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column("usage_ledger", "pool_paid")
    op.drop_column("attempts", "resources")
    op.drop_column("jobs", "sizing")
    op.drop_column("jobs", "max_runtime_s")
    op.drop_column("groups", "default_time_limit_s")
