"""image info: what pool reads from an image before it runs (docs/design/pool-decides.md §4.1)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-07 09:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("image_info", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("jobs", sa.Column("architectures", postgresql.ARRAY(sa.String(length=32)), nullable=True))
    op.create_table(
        "image_infos",
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("info", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("digest"),
    )


def downgrade() -> None:
    op.drop_table("image_infos")
    op.drop_column("jobs", "architectures")
    op.drop_column("jobs", "image_info")
