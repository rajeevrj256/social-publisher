"""google drive sources fetched on demand

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("videos",
                  sa.Column("remote_id", sa.String(length=255), nullable=True))
    op.create_index("ix_videos_remote_id", "videos", ["remote_id"])
    # Postgres enums need the value added explicitly; ALTER TYPE ADD VALUE
    # cannot run inside a transaction block on older servers.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE source_kind_enum ADD VALUE IF NOT EXISTS 'gdrive'")


def downgrade() -> None:
    op.drop_index("ix_videos_remote_id", table_name="videos")
    op.drop_column("videos", "remote_id")
