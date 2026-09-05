"""per-account oauth app credentials

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Each Google account is its own Cloud project, so the OAuth client cannot
    # live in a single global env var.
    op.add_column("account_credentials",
                  sa.Column("client_id", sa.String(length=255), nullable=True))
    op.add_column("account_credentials",
                  sa.Column("client_secret_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("account_credentials", "client_secret_encrypted")
    op.drop_column("account_credentials", "client_id")
