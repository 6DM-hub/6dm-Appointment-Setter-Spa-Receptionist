import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.appointment import Appointment
    from app.models.call_log import CallLog
    from app.models.spa_account import SpaAccount
    from app.models.user import User


class Contact(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A person on the other end of a call.

    Scoped either to the 6DM sales workspace (`owner_id` set, `tenant_id` NULL —
    these are the B2B leads) or to a spa tenant (`tenant_id` set, `owner_id`
    NULL — spa guests, which the receptionist creates with no user involved).
    """

    __tablename__ = "contacts"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(owner_id, tenant_id) = 1",
            name="ck_contacts_single_scope",
        ),
        # Two partial indexes rather than one composite: a plain
        # UNIQUE(owner_id, tenant_id, phone_number) would not deduplicate at all,
        # because Postgres treats every NULL as distinct.
        Index(
            "uq_contact_owner_phone",
            "owner_id",
            "phone_number",
            unique=True,
            postgresql_where=text("tenant_id IS NULL"),
        ),
        Index(
            "uq_contact_tenant_phone",
            "tenant_id",
            "phone_number",
            unique=True,
            postgresql_where=text("tenant_id IS NOT NULL"),
        ),
    )

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spa_accounts.id", ondelete="CASCADE"),
        index=True,
    )

    first_name: Mapped[str | None] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120))
    phone_number: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True  # E.164
    )
    email: Mapped[str | None] = mapped_column(String(255), index=True)

    # Freeform CRM metadata (source, tags, notes, external IDs)
    extra_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # --- Relationships ---
    owner: Mapped["User | None"] = relationship(back_populates="contacts")
    tenant: Mapped["SpaAccount | None"] = relationship()
    appointments: Mapped[list["Appointment"]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )
    call_logs: Mapped[list["CallLog"]] = relationship(
        back_populates="contact"
    )

    @property
    def full_name(self) -> str:
        return " ".join(filter(None, [self.first_name, self.last_name])) or "Unknown"

    def __repr__(self) -> str:
        return f"<Contact id={self.id} phone={self.phone_number}>"
