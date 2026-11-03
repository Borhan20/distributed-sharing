"""deleting jobs and groups, removing computers from groups

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03 12:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_groups_state", "groups", type_="check")
    op.create_check_constraint("ck_groups_state", "groups", "state IN ('active','expired','closed','deleted')")
    op.add_column("groups", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("groups", sa.Column("purge_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("groups", sa.Column("state_before_delete", sa.String(length=16), nullable=True))
    op.add_column("device_shares", sa.Column("removed", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("device_shares", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "run_history",
        sa.Column("attempt_id", sa.UUID(), nullable=False),
        sa.Column("device_id", sa.UUID(), nullable=False),
        sa.Column("group_name", sa.String(length=63), nullable=False),
        sa.Column("submitter", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("job_name", sa.String(length=63), nullable=False),
        sa.Column("task_index", sa.Integer(), nullable=False),
        sa.Column("image", sa.Text(), nullable=False),
        sa.Column("command", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("end_reason", sa.String(length=64), nullable=True),
        sa.Column("tier", sa.String(length=4), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("attempt_id"),
    )
    op.create_index("ix_run_history_device", "run_history", ["device_id", "created_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_run_history_device", table_name="run_history")
    op.drop_table("run_history")
    op.drop_column("device_shares", "removed_at")
    op.drop_column("device_shares", "removed")
    op.drop_column("groups", "state_before_delete")
    op.drop_column("groups", "purge_after")
    op.drop_column("groups", "deleted_at")
    op.drop_constraint("ck_groups_state", "groups", type_="check")
    # Purge or restore deleted groups before downgrading; the old constraint has no 'deleted'.
    op.create_check_constraint("ck_groups_state", "groups", "state IN ('active','expired','closed')")
