"""credits: share to earn, use to spend (docs/design/credits.md)

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-06 20:00:00
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

RATES = '{"cpu_hour": 1, "gib_hours_per_credit": 4, "gpu_hour": 10}'


def upgrade() -> None:
    op.add_column("groups", sa.Column("credits_enabled", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("groups", sa.Column("credit_start", sa.Numeric(18, 4), server_default="20", nullable=False))
    op.add_column("groups", sa.Column("credit_rates", postgresql.JSONB(astext_type=sa.Text()),
                                      server_default=sa.text(f"'{RATES}'::jsonb"), nullable=False))
    op.add_column("groups", sa.Column("out_of_credits", sa.String(length=16), server_default="lowest", nullable=False))
    op.add_column("groups", sa.Column("standby_percent", sa.Integer(), server_default="0", nullable=False))
    # Groups whose earned credits members may move in: none unless the coordinator names them.
    op.add_column("groups", sa.Column("credits_from", postgresql.ARRAY(sa.String(length=63)),
                                      server_default="{}", nullable=False))
    op.add_column("groups", sa.Column("credits_out", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("groups", sa.Column("credits_since", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint("ck_groups_out_of_credits", "groups", "out_of_credits IN ('lowest', 'wait')")
    op.create_check_constraint("ck_groups_standby_percent", "groups", "standby_percent BETWEEN 0 AND 100")
    op.add_column("attempts", sa.Column("charged", sa.Numeric(18, 4), server_default="0", nullable=False))
    op.add_column("attempts", sa.Column("charged_s", sa.Float(), server_default="0", nullable=False))
    op.add_column("attempts", sa.Column("charged_free", sa.Numeric(18, 4), server_default="0", nullable=False))
    op.add_column("attempts", sa.Column("earned_free", sa.Numeric(18, 4), server_default="0", nullable=False))
    op.add_column("devices", sa.Column("last_ready_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("device_shares", sa.Column("standby_paid_until", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "credit_balances",
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("balance", sa.Numeric(18, 4), server_default="0", nullable=False),
        sa.Column("free", sa.Numeric(18, 4), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("group_id", "user_id"),
        sa.CheckConstraint("free >= 0", name="ck_credit_balances_free"),
    )
    op.create_table(
        "credit_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("free", sa.Numeric(18, 4), server_default="0", nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("attempt_id", sa.UUID(), nullable=True),
        sa.Column("device_id", sa.UUID(), nullable=True),
        sa.Column("counterparty_id", sa.UUID(), nullable=True),
        sa.Column("request_id", sa.UUID(), nullable=True),
        sa.Column("actor_id", sa.UUID(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_credit_entries_group_user", "credit_entries", ["group_id", "user_id", "id"])
    # Entries are never changed (they go only when their group or member is deleted for good).
    op.execute("""
        CREATE FUNCTION credit_entries_never_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'credit_entries rows are never changed';
        END;
        $$;
    """)
    op.execute("""
        CREATE TRIGGER credit_entries_no_update BEFORE UPDATE ON credit_entries
        FOR EACH ROW EXECUTE FUNCTION credit_entries_never_change();
    """)
    op.create_index("ix_credit_entries_job", "credit_entries", ["job_id"])
    op.create_table(
        "credit_requests",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("group_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 4), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("granted", sa.Numeric(18, 4), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("state IN ('open', 'approved', 'declined')", name="ck_credit_requests_state"),
        sa.CheckConstraint("amount > 0", name="ck_credit_requests_amount"),
    )
    # One open request per member and group.
    op.create_index("uq_credit_requests_open", "credit_requests", ["group_id", "user_id"], unique=True,
                    postgresql_where=sa.text("state = 'open'"))


def downgrade() -> None:
    op.drop_index("uq_credit_requests_open", table_name="credit_requests")
    op.drop_table("credit_requests")
    op.execute("DROP TRIGGER IF EXISTS credit_entries_no_update ON credit_entries")
    op.execute("DROP FUNCTION IF EXISTS credit_entries_never_change()")
    op.drop_index("ix_credit_entries_job", table_name="credit_entries")
    op.drop_index("ix_credit_entries_group_user", table_name="credit_entries")
    op.drop_table("credit_entries")
    op.drop_table("credit_balances")
    op.drop_column("device_shares", "standby_paid_until")
    op.drop_column("devices", "last_ready_at")
    op.drop_column("attempts", "earned_free")
    op.drop_column("attempts", "charged_free")
    op.drop_column("attempts", "charged_s")
    op.drop_column("attempts", "charged")
    op.drop_constraint("ck_groups_standby_percent", "groups", type_="check")
    op.drop_constraint("ck_groups_out_of_credits", "groups", type_="check")
    for col in ("credits_since", "credits_out", "credits_from", "standby_percent", "out_of_credits", "credit_rates",
                "credit_start", "credits_enabled"):
        op.drop_column("groups", col)
