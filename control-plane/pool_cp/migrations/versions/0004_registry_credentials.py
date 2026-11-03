"""group registry credentials for private images

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-03 18:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_registry_credentials",
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("registry_host", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("updated_by", sa.UUID(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("group_id", "registry_host"),
    )


def downgrade() -> None:
    op.drop_table("group_registry_credentials")
