"""multi-tenant SaaS engine with RBAC

Adds the `spa_accounts` tenant table, `role`/`tenant_id` on users, and a nullable
`tenant_id` on every tenant-owned table (contacts, appointments, call_logs).

BACKFILL NOTE — every pre-existing user is promoted to `super_admin`. Those rows
are 6DM's own single-tenant workspace: their contacts, appointments and call
logs all land in the sales workspace (`tenant_id IS NULL`), and the
`ck_users_role_tenant_consistency` constraint requires any non-super_admin to
point at a spa, of which there are none yet. Demote and assign tenants
deliberately after the first spa accounts exist.

Revision ID: b7f2a91c4d30
Revises: 6148cebb7309
Create Date: 2026-08-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7f2a91c4d30"
down_revision: Union[str, Sequence[str], None] = "6148cebb7309"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


USER_ROLES = ("super_admin", "spa_admin", "spa_staff")
BOOKING_PROVIDERS = (
    "google_calendar",
    "mindbody",
    "mangomint",
    "square",
    "vagaro",
    "zenoti",
)

user_role = postgresql.ENUM(*USER_ROLES, name="user_role", create_type=False)
booking_provider = postgresql.ENUM(
    *BOOKING_PROVIDERS, name="booking_provider", create_type=False
)


def upgrade() -> None:
    bind = op.get_bind()
    user_role.create(bind, checkfirst=True)
    booking_provider.create(bind, checkfirst=True)

    # --- Tenants ---------------------------------------------------------- #
    op.create_table(
        "spa_accounts",
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("twilio_phone_number", sa.String(length=32), nullable=True),
        sa.Column("grok_system_prompt", sa.Text(), nullable=True),
        sa.Column(
            "business_hours",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "services",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "staff",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "timezone", sa.String(length=64), server_default="UTC", nullable=False
        ),
        sa.Column(
            "booking_provider",
            booking_provider,
            server_default="google_calendar",
            nullable=False,
        ),
        sa.Column(
            "booking_config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("twiml_voice", sa.String(length=64), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_spa_accounts_twilio_phone_number"),
        "spa_accounts",
        ["twilio_phone_number"],
        unique=True,
    )

    # --- Users: role + tenant --------------------------------------------- #
    op.add_column("users", sa.Column("role", user_role, nullable=True))
    op.add_column("users", sa.Column("tenant_id", sa.UUID(), nullable=True))
    # See BACKFILL NOTE above.
    op.execute("UPDATE users SET role = 'super_admin' WHERE role IS NULL")
    op.execute("UPDATE users SET is_superuser = true")
    op.alter_column(
        "users",
        "role",
        existing_type=user_role,
        nullable=False,
        server_default="spa_admin",
    )
    op.create_index(op.f("ix_users_role"), "users", ["role"], unique=False)
    op.create_index(op.f("ix_users_tenant_id"), "users", ["tenant_id"], unique=False)
    op.create_foreign_key(
        "fk_users_tenant_id_spa_accounts",
        "users",
        "spa_accounts",
        ["tenant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_users_role_tenant_consistency",
        "users",
        "(role = 'super_admin' AND tenant_id IS NULL)"
        " OR (role <> 'super_admin' AND tenant_id IS NOT NULL)",
    )

    # --- Contacts: tenant scope ------------------------------------------- #
    op.add_column("contacts", sa.Column("tenant_id", sa.UUID(), nullable=True))
    op.alter_column("contacts", "owner_id", existing_type=sa.UUID(), nullable=True)
    op.create_index(
        op.f("ix_contacts_tenant_id"), "contacts", ["tenant_id"], unique=False
    )
    op.create_foreign_key(
        "fk_contacts_tenant_id_spa_accounts",
        "contacts",
        "spa_accounts",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Replace the plain UNIQUE(owner_id, phone_number) with two partial indexes,
    # one per scope: a composite over both nullable owner columns would never
    # deduplicate, since Postgres treats every NULL as distinct.
    op.drop_constraint("uq_contact_owner_phone", "contacts", type_="unique")
    op.create_index(
        "uq_contact_owner_phone",
        "contacts",
        ["owner_id", "phone_number"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NULL"),
    )
    op.create_index(
        "uq_contact_tenant_phone",
        "contacts",
        ["tenant_id", "phone_number"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )
    op.create_check_constraint(
        "ck_contacts_single_scope", "contacts", "num_nonnulls(owner_id, tenant_id) = 1"
    )

    # --- Appointments: tenant scope + provider mirror --------------------- #
    op.add_column("appointments", sa.Column("tenant_id", sa.UUID(), nullable=True))
    op.add_column(
        "appointments", sa.Column("booking_provider", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "appointments",
        sa.Column("external_booking_id", sa.String(length=255), nullable=True),
    )
    op.alter_column("appointments", "user_id", existing_type=sa.UUID(), nullable=True)
    op.create_index(
        op.f("ix_appointments_tenant_id"), "appointments", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_appointments_external_booking_id"),
        "appointments",
        ["external_booking_id"],
        unique=False,
    )
    op.create_index(
        "ix_appointments_tenant_start", "appointments", ["tenant_id", "start_time"]
    )
    op.create_foreign_key(
        "fk_appointments_tenant_id_spa_accounts",
        "appointments",
        "spa_accounts",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_appointments_single_scope",
        "appointments",
        "num_nonnulls(user_id, tenant_id) = 1",
    )

    # --- Call logs: tenant scope ------------------------------------------ #
    # No single-scope constraint here: an inbound call to a number no tenant
    # claims is legitimately unattributed, and dropping that row would lose the
    # only record of a misrouted call.
    op.add_column("call_logs", sa.Column("tenant_id", sa.UUID(), nullable=True))
    op.create_index(
        op.f("ix_call_logs_tenant_id"), "call_logs", ["tenant_id"], unique=False
    )
    op.create_foreign_key(
        "fk_call_logs_tenant_id_spa_accounts",
        "call_logs",
        "spa_accounts",
        ["tenant_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_call_logs_tenant_id_spa_accounts", "call_logs", type_="foreignkey")
    op.drop_index(op.f("ix_call_logs_tenant_id"), table_name="call_logs")
    op.drop_column("call_logs", "tenant_id")

    op.drop_constraint("ck_appointments_single_scope", "appointments", type_="check")
    op.drop_constraint(
        "fk_appointments_tenant_id_spa_accounts", "appointments", type_="foreignkey"
    )
    op.drop_index("ix_appointments_tenant_start", table_name="appointments")
    op.drop_index(op.f("ix_appointments_external_booking_id"), table_name="appointments")
    op.drop_index(op.f("ix_appointments_tenant_id"), table_name="appointments")
    # Rows created by a spa have no user_id and cannot survive the NOT NULL.
    op.execute("DELETE FROM appointments WHERE user_id IS NULL")
    op.alter_column("appointments", "user_id", existing_type=sa.UUID(), nullable=False)
    op.drop_column("appointments", "external_booking_id")
    op.drop_column("appointments", "booking_provider")
    op.drop_column("appointments", "tenant_id")

    op.drop_constraint("ck_contacts_single_scope", "contacts", type_="check")
    op.drop_index("uq_contact_tenant_phone", table_name="contacts")
    op.drop_index("uq_contact_owner_phone", table_name="contacts")
    op.drop_constraint("fk_contacts_tenant_id_spa_accounts", "contacts", type_="foreignkey")
    op.drop_index(op.f("ix_contacts_tenant_id"), table_name="contacts")
    op.execute("DELETE FROM contacts WHERE owner_id IS NULL")
    op.alter_column("contacts", "owner_id", existing_type=sa.UUID(), nullable=False)
    op.create_unique_constraint(
        "uq_contact_owner_phone", "contacts", ["owner_id", "phone_number"]
    )
    op.drop_column("contacts", "tenant_id")

    op.drop_constraint("ck_users_role_tenant_consistency", "users", type_="check")
    op.drop_constraint("fk_users_tenant_id_spa_accounts", "users", type_="foreignkey")
    op.drop_index(op.f("ix_users_tenant_id"), table_name="users")
    op.drop_index(op.f("ix_users_role"), table_name="users")
    op.drop_column("users", "tenant_id")
    op.drop_column("users", "role")

    op.drop_index(op.f("ix_spa_accounts_twilio_phone_number"), table_name="spa_accounts")
    op.drop_table("spa_accounts")

    bind = op.get_bind()
    booking_provider.drop(bind, checkfirst=True)
    user_role.drop(bind, checkfirst=True)
