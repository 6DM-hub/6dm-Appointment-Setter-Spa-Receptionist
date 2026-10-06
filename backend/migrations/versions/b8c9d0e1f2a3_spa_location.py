"""Add an optional business location to spa accounts."""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("spa_accounts", sa.Column("location", sa.String(length=512), nullable=True))


def downgrade() -> None:
    op.drop_column("spa_accounts", "location")