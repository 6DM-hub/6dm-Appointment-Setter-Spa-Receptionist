"""Durable tenant-scoped failed-booking inbox."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
revision = "a104_booking_escalations"
down_revision = "a103_smart_enhancements"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("booking_escalations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("spa_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("call_reference", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("details", sa.JSON, nullable=False),
        sa.Column("delivery", sa.JSON, nullable=False),
        sa.Column("history", sa.JSON, nullable=False),
        sa.Column("delivery_status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "call_reference", name="uq_escalation_tenant_call"))
    op.create_index("ix_booking_escalations_tenant_id", "booking_escalations", ["tenant_id"])


def downgrade():
    op.drop_table("booking_escalations")
