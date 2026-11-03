"""built-in registry: image clean-up records and a per-group retention

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-04 09:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("groups", sa.Column("registry_retention_days", sa.Integer(), nullable=True))
    op.create_table(
        "registry_images",
        sa.Column("repository", sa.String(length=255), nullable=False),
        sa.Column("digest", sa.String(length=80), nullable=False),
        sa.Column("tag", sa.String(length=128), nullable=True),
        sa.Column("group_id", sa.UUID(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("repository", "digest"),
    )


def downgrade() -> None:
    op.drop_table("registry_images")
    op.drop_column("groups", "registry_retention_days")
