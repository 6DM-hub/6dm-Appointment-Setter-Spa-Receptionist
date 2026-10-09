"""Separate establishment master authority from platform administration."""
from alembic import op
import sqlalchemy as sa

revision = "a102_business_master"
down_revision = "a101_cara_manager"
branch_labels = None
depends_on = None


def upgrade():
    from sqlalchemy.dialects.postgresql import JSONB
    op.add_column("cara_deliveries", sa.Column("booking_session", JSONB(), nullable=True))
    op.add_column("users", sa.Column("is_business_master", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_check_constraint("ck_users_business_master_scope", "users",
                               "NOT is_business_master OR (role = 'spa_admin' AND tenant_id IS NOT NULL)")


def downgrade():
    op.drop_column("cara_deliveries", "booking_session")
    op.drop_constraint("ck_users_business_master_scope", "users", type_="check")
    op.drop_column("users", "is_business_master")
