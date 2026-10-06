"""Add protected booking configuration and detected call language."""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f60a11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("call_logs", sa.Column("primary_language", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("call_logs", "primary_language")