"""human-readable remote link on videos

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("videos", sa.Column("remote_url", sa.Text(), nullable=True))
    # Backfill from the ids already indexed.
    op.execute("""
        UPDATE videos
           SET remote_url = 'https://drive.google.com/file/d/' || remote_id || '/view'
         WHERE remote_id IS NOT NULL AND remote_url IS NULL
    """)


def downgrade() -> None:
    op.drop_column("videos", "remote_url")
