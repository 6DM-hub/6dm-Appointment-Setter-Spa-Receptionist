import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.call_log import CallLog
    from app.models.contact import Contact
    from app.models.spa_account import SpaAccount
    from app.models.user import User


class AppointmentStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class CardStatus(str, enum.Enum):
    """Whether a usable saved payment method is already on file.

    This is not an appointment lifecycle status and it is not a charge.
    """

    NOT_REQUIRED = "not_required"
    NOT_SUPPORTED = "not_supported"
    UNKNOWN = "unknown"
    PENDING_CARD = "pending_card"
    CARD_CONFIRMED = "card_confirmed"
    FAILED = "failed"


class Appointment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """The local source of truth for a booking.

    Always written, whichever provider the tenant uses; `booking_provider` /
    `external_booking_id` record where it was additionally mirrored (Dominic's
    Google Calendar for sales, the spa's Mindbody/Square/... calendar inbound).
    """

    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(user_id, tenant_id) = 1",
            name="ck_appointments_single_scope",
        ),
        Index("ix_appointments_user_start", "user_id", "start_time"),
        Index("ix_appointments_tenant_start", "tenant_id", "start_time"),
        # One appointment per *booking intent*, not per call. A caller who
        # explicitly asks for two separate appointments gets two intents and
        # therefore two rows; a caller who changes their mind five times keeps
        # one intent and one row. Replaces an earlier unique index on
        # source_call_id, which forbade the first case and turned a repeated
        # confirmation into an IntegrityError instead of a no-op.
        Index(
            "uq_appointments_booking_intent",
            "booking_intent_key",
            unique=True,
            postgresql_where="booking_intent_key IS NOT NULL",
        ),
    )

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spa_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    contact_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Optional back-reference to the call that created this appointment
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("call_logs.id", ondelete="SET NULL"),
        index=True,
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(
            AppointmentStatus,
            name="appointment_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=AppointmentStatus.SCHEDULED,
        server_default=AppointmentStatus.SCHEDULED.value,
        index=True,
    )
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Where this booking was mirrored, and its id over there (if anywhere).
    booking_provider: Mapped[str | None] = mapped_column(String(32))
    external_booking_id: Mapped[str | None] = mapped_column(String(255), index=True)

    # Idempotency key for the conversation that produced this booking, shaped
    # "<call_sid>:<booking_id>" (see app/services/booking_state.py). Confirming
    # the same intent twice — a repeated "yes", a retried tool call — resolves
    # to this row instead of inserting another. Null for appointments created
    # outside a call, e.g. through the dashboard.
    booking_intent_key: Mapped[str | None] = mapped_column(String(128))

    # Saved-card result for this visit. Independent of `status`.
    card_status: Mapped[CardStatus] = mapped_column(
        Enum(
            CardStatus,
            name="card_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=CardStatus.UNKNOWN,
        server_default=CardStatus.UNKNOWN.value,
    )

    # --- Relationships ---
    user: Mapped["User | None"] = relationship(back_populates="appointments")
    tenant: Mapped["SpaAccount | None"] = relationship()
    contact: Mapped["Contact"] = relationship(back_populates="appointments")
    source_call: Mapped["CallLog | None"] = relationship(
        foreign_keys=[source_call_id]
    )

    def __repr__(self) -> str:
        return f"<Appointment id={self.id} status={self.status} start={self.start_time}>"
