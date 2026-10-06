import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.models.spa_account import SpaAccount
    from app.models.user import User


class CallDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class CallStatus(str, enum.Enum):
    QUEUED = "queued"
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BUSY = "busy"
    FAILED = "failed"
    NO_ANSWER = "no_answer"
    CANCELLED = "cancelled"


class CallLog(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "call_logs"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    # Set for every call answered on a spa's number. NULL means the 6DM sales
    # workspace (outbound B2B), or an inbound call to an unrecognised number.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spa_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("contacts.id", ondelete="SET NULL"),
        index=True,
    )

    twilio_call_sid: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    direction: Mapped[CallDirection] = mapped_column(
        Enum(
            CallDirection,
            name="call_direction",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        index=True,
    )

    status: Mapped[CallStatus] = mapped_column(
        Enum(
            CallStatus,
            name="call_status",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=CallStatus.QUEUED,
        server_default=CallStatus.QUEUED.value,
        index=True,
    )

    from_number: Mapped[str] = mapped_column(String(32), nullable=False)
    to_number: Mapped[str] = mapped_column(String(32), nullable=False)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    transcript: Mapped[str | None] = mapped_column(Text)
    recording_url: Mapped[str | None] = mapped_column(String(512))
    ai_summary: Mapped[str | None] = mapped_column(Text)
    primary_language: Mapped[str | None] = mapped_column(String(64))

    # Structured extraction from the Grok pipeline (intent, entities, sentiment)
    ai_analysis: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # --- Relationships ---
    user: Mapped["User | None"] = relationship(back_populates="call_logs")
    tenant: Mapped["SpaAccount | None"] = relationship()
    contact: Mapped["Contact | None"] = relationship(back_populates="call_logs")

    def __repr__(self) -> str:
        return f"<CallLog sid={self.twilio_call_sid} dir={self.direction} status={self.status}>"