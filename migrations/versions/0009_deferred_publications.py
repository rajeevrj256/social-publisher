"""temporary "not today" rejections

A deferred publication keeps blocking its video until available_after passes,
then selection lets the video through again and reserve_publication reuses the
row in place -- the unique constraint on (video, account, platform) allows only
one row, so revival cannot be an insert.

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres refuses to use a new enum label in the transaction that adds it,
    # but adding it alone is fine; nothing here writes the value.
    op.execute("ALTER TYPE publication_status_enum ADD VALUE IF NOT EXISTS 'deferred'")
    op.add_column(
        "publications",
        sa.Column("available_after", sa.DateTime(timezone=True), nullable=True),
    )
    # Selection filters on (status, available_after) on every candidate query.
    op.create_index("ix_publications_available_after", "publications",
                    ["available_after"])


def downgrade() -> None:
    op.drop_index("ix_publications_available_after", table_name="publications")
    op.drop_column("publications", "available_after")
    # The enum label is intentionally left in place: dropping a value requires
    # rebuilding the type, and any row still holding it would be orphaned.
