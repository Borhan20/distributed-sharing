"""apps: jobs that listen on ports (docs/design/pool-run.md step 2)

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-07 15:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("ports", postgresql.ARRAY(sa.Integer()), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "ports")
