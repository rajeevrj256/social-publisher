"""coordinate themes by default

Revision ID: 0007
Revises: 0006
"""
import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # New themes coordinate unless explicitly told otherwise.
    op.alter_column("themes", "coordinated", server_default=sa.true())
    # Existing themes too: sharing a theme across accounts is meant to publish
    # the same video, and nobody opted into the old drift.
    op.execute("UPDATE themes SET coordinated = true")


def downgrade() -> None:
    op.alter_column("themes", "coordinated", server_default=sa.false())
