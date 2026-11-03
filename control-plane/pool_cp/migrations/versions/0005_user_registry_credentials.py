"""members' own registry credentials for private images

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-03 21:00:00
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_registry_credentials",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("registry_host", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "registry_host"),
    )


def downgrade() -> None:
    op.drop_table("user_registry_credentials")
