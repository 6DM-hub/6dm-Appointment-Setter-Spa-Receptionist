"""Represent a verified request awaiting provider approval.

Upgrade is additive. Downgrade normalizes rows but deliberately retains the
unused PostgreSQL enum label; PostgreSQL cannot simply drop an enum value.
Stop writers and export affected rows before downgrade. Restoring old code
loses the approval distinction, so keep affected appointments out of caller
traffic until staff reconciles them. See the handoff rollback procedure.
"""
from alembic import op

revision = "a106_provider_approval"
down_revision = "a105_square_catalog"
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE appointment_status ADD VALUE IF NOT EXISTS 'awaiting_provider_approval'")


def downgrade():
    op.execute("UPDATE appointments SET status = 'scheduled' WHERE status = 'awaiting_provider_approval'")
