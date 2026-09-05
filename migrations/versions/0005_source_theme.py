"""source remembers its theme

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sources",
                  sa.Column("videos_theme_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_sources_theme", "sources", "themes",
                          ["videos_theme_id"], ["id"], ondelete="SET NULL")
    # Backfill from what each source's videos already carry.
    op.execute("""
        UPDATE sources s SET videos_theme_id = (
            SELECT v.theme_id FROM videos v
             WHERE v.source_id = s.id AND v.theme_id IS NOT NULL
             LIMIT 1)
    """)


def downgrade() -> None:
    op.drop_constraint("fk_sources_theme", "sources", type_="foreignkey")
    op.drop_column("sources", "videos_theme_id")
