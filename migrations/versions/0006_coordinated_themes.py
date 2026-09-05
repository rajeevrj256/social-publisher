"""coordinated themes publish the same video everywhere

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("themes", sa.Column("coordinated", sa.Boolean(),
                                      nullable=False, server_default=sa.false()))
    op.add_column("publications", sa.Column("group_key", sa.String(length=64),
                                            nullable=True))
    op.create_index("ix_pub_group_key", "publications", ["group_key"])


def downgrade() -> None:
    op.drop_index("ix_pub_group_key", table_name="publications")
    op.drop_column("publications", "group_key")
    op.drop_column("themes", "coordinated")
