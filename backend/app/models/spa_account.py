import enum
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, Enum, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User
    from app.models.google_calendar_connection import GoogleCalendarConnection


class BookingProvider(str, enum.Enum):
    """Calendar/booking system a spa's appointments are mirrored into."""

    GOOGLE_CALENDAR = "google_calendar"
    MINDBODY = "mindbody"
    MANGOMINT = "mangomint"
    SQUARE = "square"
    VAGARO = "vagaro"
    ZENOTI = "zenoti"


class VoiceEngine(str, enum.Enum):
    """How this tenant's inbound calls are answered.

    `TWILIO_TTS` is the <Gather>/<Say> pipeline: Twilio transcribes the caller,
    Grok writes a reply, Twilio speaks it. `XAI_REALTIME` bridges the call audio
    straight into xAI's speech-to-speech socket, which hears and speaks itself —
    markedly more natural, at the cost of a live audio bridge.
    """

    TWILIO_TTS = "twilio_tts"
    XAI_REALTIME = "xai_realtime"


class SpaAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single spa tenant.

    Onboarding a new spa is purely a matter of inserting one of these rows: the
    inbound Twilio webhook resolves the tenant from `twilio_phone_number`, the
    receptionist persona comes from `grok_system_prompt` + `services`/`staff`/
    `business_hours`, and bookings route through `booking_provider`. No code
    changes are required per spa.
    """

    __tablename__ = "spa_accounts"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str | None] = mapped_column(String(512))

    # The dialed number that identifies this tenant on inbound calls.
    twilio_phone_number: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True
    )

    # Tenant-specific receptionist persona. Prepended to the shared voice rules.
    grok_system_prompt: Mapped[str | None] = mapped_column(Text)

    # {"mon": [{"open": "09:00", "close": "18:00"}], ..., "sun": []}
    business_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # [{"name": "Deep tissue massage", "duration_minutes": 60, "price": "120"}]
    services: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # [{"name": "Riley", "role": "Massage therapist"}]
    staff: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )

    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="America/Chicago", server_default="America/Chicago"
    )

    booking_provider: Mapped[BookingProvider] = mapped_column(
        Enum(
            BookingProvider,
            name="booking_provider",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=BookingProvider.GOOGLE_CALENDAR,
        server_default=BookingProvider.GOOGLE_CALENDAR.value,
    )
    # Provider credentials / location ids. Shape is provider-specific; see
    # app/services/booking_adapters/providers/.
    booking_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # Twilio <Say> voice override for this tenant. Only used by TWILIO_TTS;
    # the realtime bridge picks its voice with settings.XAI_VOICE_ID.
    twiml_voice: Mapped[str | None] = mapped_column(String(64))

    voice_engine: Mapped[VoiceEngine] = mapped_column(
        Enum(
            VoiceEngine,
            name="voice_engine",
            native_enum=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
        default=VoiceEngine.TWILIO_TTS,
        server_default=VoiceEngine.TWILIO_TTS.value,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Authoritative receptionist copy. The voice model must look these up
    # through `lookup_spa_facts` rather than inventing them.
    description: Mapped[str | None] = mapped_column(Text)
    public_phone: Mapped[str | None] = mapped_column(String(32))
    cancellation_policy: Mapped[str | None] = mapped_column(Text)
    amenities: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    packages: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # [{"base_service": "HydroLux5 Facial - Face Only", "allowed_upsells": ["Neck upgrade"]}]
    upsell_rules: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    # Booking CC: spa-configured card requirement. collection_mode none|at_spa|square_link.
    # The LLM never processes raw card numbers.
    payment_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default='{"card_required": false, "collection_mode": "none"}',
    )
    # {"sms_destinations": [], "email_destinations": [], "events": [...]}
    # Empty destinations mean notifications are skipped. Never hard-code staff.
    notification_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Informational only. Missing keys stay unknown and do not allow or block a cancel.
    booking_policies: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    enhancement_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )

    # --- Relationships ---
    users: Mapped[list["User"]] = relationship(back_populates="tenant")
    google_calendar_connection: Mapped["GoogleCalendarConnection | None"] = relationship(
        back_populates="spa",
        uselist=False,
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<SpaAccount id={self.id} name={self.name!r}>"
