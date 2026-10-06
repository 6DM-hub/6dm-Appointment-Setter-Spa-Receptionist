import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.spa_account import SpaAccount


class GoogleCalendarConnection(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """OAuth authorization owned by exactly one spa tenant."""

    __tablename__ = "google_calendar_connections"
    __table_args__ = (UniqueConstraint("spa_id", name="uq_google_calendar_connection_spa"),)

    spa_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("spa_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    google_account_email: Mapped[str | None] = mapped_column(String(320))
    selected_calendar_id: Mapped[str | None] = mapped_column(String(512))
    # This JSON object is Fernet-encrypted before it is persisted.
    credentials: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    scopes: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    status: Mapped[str] = mapped_column(
        String(64), nullable=False, default="connected", server_default="connected"
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    spa: Mapped["SpaAccount"] = relationship(back_populates="google_calendar_connection")