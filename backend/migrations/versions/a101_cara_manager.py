"""Cara owner assistant, immutable approvals and test reactivation."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "a101_cara_manager"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def base():
    return [
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("tenant_id", pg.UUID(as_uuid=True), sa.ForeignKey("spa_accounts.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    ]


def upgrade():
    op.create_table("cara_preferences", *base(),
        sa.Column("approved_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("value", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("tenant_id"))
    op.create_table("cara_campaigns", *base(),
        sa.Column("requested_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("proposal", pg.JSONB(), nullable=False),
        sa.Column("proposal_hash", sa.String(64), nullable=False),
        sa.Column("approved_hash", sa.String(64)),
        sa.Column("approved_by", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.create_table("cara_deliveries", *base(),
        sa.Column("campaign_id", pg.UUID(as_uuid=True), sa.ForeignKey("cara_campaigns.id"), nullable=False),
        sa.Column("contact_id", pg.UUID(as_uuid=True), sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("response", sa.Text()),
        sa.Column("booking", pg.JSONB()),
        sa.UniqueConstraint("campaign_id", "contact_id", name="uq_cara_campaign_contact"))
    op.create_table("cara_packages", *base(),
        sa.Column("campaign_id", pg.UUID(as_uuid=True), sa.ForeignKey("cara_campaigns.id"), nullable=False),
        sa.Column("contact_id", pg.UUID(as_uuid=True), sa.ForeignKey("contacts.id"), nullable=False),
        sa.Column("offer", pg.JSONB(), nullable=False),
        sa.UniqueConstraint("campaign_id", "contact_id", name="uq_cara_package_contact"))
    op.create_table("cara_audit", *base(),
        sa.Column("actor_id", pg.UUID(as_uuid=True), sa.ForeignKey("users.id")),
        sa.Column("campaign_id", pg.UUID(as_uuid=True), sa.ForeignKey("cara_campaigns.id")),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("details", pg.JSONB(), nullable=False))
    for table in ("cara_campaigns", "cara_deliveries", "cara_packages", "cara_audit"):
        op.create_index("ix_" + table + "_tenant_id", table, ["tenant_id"])
    op.create_index("ix_cara_deliveries_campaign_id", "cara_deliveries", ["campaign_id"])
    op.create_index("ix_cara_deliveries_contact_id", "cara_deliveries", ["contact_id"])


def downgrade():
    for table in ("cara_audit", "cara_packages", "cara_deliveries", "cara_campaigns", "cara_preferences"):
        op.drop_table(table)
