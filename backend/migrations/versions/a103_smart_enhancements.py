"""Optional tenant-scoped verified enhancement offers."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID
revision = "a103_smart_enhancements"
down_revision = "a102_business_master"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("spa_accounts", sa.Column("enhancement_settings", JSONB, nullable=False, server_default="{}"))
    op.create_table("enhancement_offers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", UUID(as_uuid=True), sa.ForeignKey("spa_accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("intent_key", sa.String(128), nullable=False),
        sa.Column("customer_key", sa.String(64)),
        sa.Column("base_service", sa.String(255), nullable=False),
        sa.Column("target_service", sa.String(255)),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("incremental_minor", sa.Integer), sa.Column("currency", sa.String(3)),
        sa.Column("facts", JSONB, nullable=False, server_default="{}"),
        sa.Column("external_booking_id", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "intent_key", name="uq_enhancement_intent"))
    op.create_index("ix_enhancement_offers_tenant_id", "enhancement_offers", ["tenant_id"])
    op.create_index("ix_enhancement_offers_customer_key", "enhancement_offers", ["customer_key"])

def downgrade():
    op.drop_table("enhancement_offers")
    op.drop_column("spa_accounts", "enhancement_settings")
