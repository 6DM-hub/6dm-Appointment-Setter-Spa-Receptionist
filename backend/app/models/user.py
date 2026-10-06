import enum
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, CheckConstraint, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.call_log import CallLog
    from app.models.contact import Contact
    from app.models.spa_account import SpaAccount


class UserRole(str, enum.Enum):
    """Who a principal is, and therefore which product surface they can reach.

    - SUPER_ADMIN: 6DM staff. Owns the outbound B2B Sales Agent (leads, outbound
      campaigns, Dominic's calendar) and can inspect any spa tenant.
    - SPA_ADMIN:   owns one spa tenant; may edit that spa's receptionist config.
    - SPA_STAFF:   read-only member of one spa tenant.
    """

    SUPER_ADMIN = "super_admin"
    SPA_ADMIN = "spa_admin"
    SPA_STAFF = "spa_staff"


SPA_ROLES = frozenset({UserRole.SPA_ADMIN, UserRole.SPA_STAFF})


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "users"
    __table_args__ = (
        # A super admin is platform-level and must not be pinned to a tenant;
        # every spa role must be. Enforced in the DB so a bad INSERT can never
        # produce a principal whose scope is ambiguous.
        CheckConstraint(
            "(role = 'super_admin' AND tenant_id IS NULL)"
            " OR (role <> 'super_admin' AND tenant_id IS NOT NULL)",
            name="ck_users_role_tenant_consistency",
        ),
    )

    email: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=UserRole.SPA_ADMIN,
        server_default=UserRole.SPA_ADMIN.value,
        index=True,
    )

    # The spa this user belongs to. NULL for super_admin (platform scope).
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spa_accounts.id", ondelete="SET NULL"),
        index=True,
    )

    # Caller ID for the 6DM outbound sales workspace. Spa inbound numbers live
    # on SpaAccount.twilio_phone_number instead.
    twilio_phone_number: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True
    )

    workspace_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # --- Relationships ---
    tenant: Mapped["SpaAccount | None"] = relationship(back_populates="users")
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan", lazy="selectin"
    )
    appointments: Mapped[list["Appointment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    call_logs: Mapped[list["CallLog"]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_super_admin(self) -> bool:
        return self.role == UserRole.SUPER_ADMIN

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"
