"""shared schedules owning multiple accounts

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        sa.Column("publish_time", sa.Time(), nullable=False),
        sa.Column("day_of_week", sa.Integer(), nullable=True),
        sa.Column("videos_per_day", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("timezone", sa.String(length=64), nullable=False,
                  server_default="Asia/Kolkata"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("theme_id", sa.Integer(),
                  sa.ForeignKey("themes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("day_of_week IS NULL OR (day_of_week BETWEEN 0 AND 6)",
                           name="ck_schedules_dow"),
        sa.CheckConstraint("videos_per_day >= 1", name="ck_schedules_per_day"),
    )
    op.create_index("ix_schedules_enabled", "schedules", ["enabled"])

    op.create_table(
        "schedule_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("schedule_id", sa.Integer(),
                  sa.ForeignKey("schedules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("schedule_id", "account_id", name="uq_schedule_account"),
    )
    op.create_index("ix_schedule_accounts_account", "schedule_accounts",
                    ["account_id"])


def downgrade() -> None:
    op.drop_table("schedule_accounts")
    op.drop_table("schedules")
