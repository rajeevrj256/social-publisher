"""initial schema

Revision ID: 0001
Revises:
"""
from alembic import op

from src.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The declarative model is the single source of truth for the schema;
    # generating from it avoids the two drifting apart on day one.
    Base.metadata.create_all(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
