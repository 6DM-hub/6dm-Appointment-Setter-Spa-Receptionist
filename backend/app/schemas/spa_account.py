import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.models.spa_account import BookingProvider, VoiceEngine
from app.schemas.common import ORMModel
from app.schemas.enhancements import EnhancementSettings

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class BusinessHoursWindow(BaseModel):
    open: str = Field(..., examples=["09:00"])
    close: str = Field(..., examples=["18:00"])

    @field_validator("open", "close")
    @classmethod
    def validate_time(cls, v: str) -> str:
        if not _TIME_RE.match(v):
            raise ValueError("times must be 24-hour HH:MM, e.g. 09:00")
        return v


class SpaService(BaseModel):
    square_variation_id: str | None = Field(None, max_length=255)
    square_variation_version: int | None = None
    name: str = Field(..., max_length=255)
    duration_minutes: int = Field(60, ge=5, le=600)
    price: str | None = Field(None, max_length=32)
    description: str | None = Field(None, max_length=1000)


class SpaStaffMember(BaseModel):
    name: str = Field(..., max_length=255)
    role: str | None = Field(None, max_length=255)
    services: list[str] = Field(default_factory=list)


class SpaUpsellRule(BaseModel):
    base_service: str = Field(..., max_length=255)
    allowed_upsells: list[str] = Field(default_factory=list)


class SpaPaymentPolicy(BaseModel):
    """Booking CC configuration: whether a card is required, and how.

    collection_mode:
      none               — do not collect a card on the call (default)
      at_spa             — tell the caller a card is taken at the spa
      square_link        — Square-hosted payment; never spoken PAN/CVV
      secure_voice_card  — Twilio Pay only; the AI never hears PAN or CVV
      secure_sms_link    — text a one-time page that saves a card on file; no charge
    """

    card_required: bool = False
    collection_mode: str = "none"
    script: str | None = Field(None, max_length=1000)

    @field_validator("collection_mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        allowed = {"none", "at_spa", "square_link", "secure_voice_card", "secure_sms_link"}
        value = (v or "none").strip().lower()
        if value not in allowed:
            raise ValueError(f"collection_mode must be one of {sorted(allowed)}")
        return value


def _normalize_e164(v: str | None) -> str | None:
    if v is None:
        return None
    cleaned = re.sub(r"[\s\-()]", "", v.strip())
    if not cleaned.startswith("+"):
        raise ValueError("twilio_phone_number must be E.164, e.g. +15551234567")
    return cleaned


def _validate_hours(v: dict[str, Any]) -> dict[str, Any]:
    unknown = set(v) - set(WEEKDAYS)
    if unknown:
        raise ValueError(
            f"unknown day keys {sorted(unknown)}; expected any of {list(WEEKDAYS)}"
        )
    for day, windows in v.items():
        if not isinstance(windows, list):
            raise ValueError(f"business_hours[{day}] must be a list of windows")
        for window in windows:
            BusinessHoursWindow.model_validate(window)
    return v


class SpaAccountBase(BaseModel):
    name: str = Field(..., max_length=255)
    location: str | None = Field(None, max_length=512)
    twilio_phone_number: str | None = Field(None, max_length=32)
    grok_system_prompt: str | None = Field(None, max_length=8000)
    business_hours: dict[str, list[BusinessHoursWindow]] = Field(default_factory=dict)
    services: list[SpaService] = Field(default_factory=list)
    staff: list[SpaStaffMember] = Field(default_factory=list)
    timezone: str = Field("America/Chicago", max_length=64)
    booking_provider: BookingProvider = BookingProvider.GOOGLE_CALENDAR
    twiml_voice: str | None = Field(None, max_length=64)
    voice_engine: VoiceEngine = VoiceEngine.TWILIO_TTS
    description: str | None = Field(None, max_length=4000)
    public_phone: str | None = Field(None, max_length=32)
    cancellation_policy: str | None = Field(None, max_length=4000)
    amenities: list[str] = Field(default_factory=list)
    packages: list[Any] = Field(default_factory=list)
    enhancement_settings: EnhancementSettings = Field(default_factory=EnhancementSettings)
    upsell_rules: list[SpaUpsellRule] = Field(default_factory=list)
    payment_policy: SpaPaymentPolicy = Field(default_factory=SpaPaymentPolicy)
    notification_settings: dict[str, Any] = Field(default_factory=dict)
    booking_policies: dict[str, Any] = Field(default_factory=dict)

    @field_validator("enhancement_settings", mode="before")
    @classmethod
    def default_enhancements(cls, value):
        return value or {}

    @field_validator("twilio_phone_number")
    @classmethod
    def normalize_phone(cls, v: str | None) -> str | None:
        return _normalize_e164(v)

    @field_validator("business_hours", mode="before")
    @classmethod
    def check_hours(cls, v: Any) -> Any:
        return _validate_hours(v) if isinstance(v, dict) else v


class SpaAccountCreate(SpaAccountBase):
    # Provider credentials / location ids. Never returned by the read schema.
    booking_config: dict[str, Any] = Field(default_factory=dict)


class SpaAccountUpdate(BaseModel):
    """Every field optional; a PATCH from the spa's own settings screen."""

    name: str | None = Field(None, max_length=255)
    location: str | None = Field(None, max_length=512)
    twilio_phone_number: str | None = Field(None, max_length=32)
    grok_system_prompt: str | None = Field(None, max_length=8000)
    business_hours: dict[str, list[BusinessHoursWindow]] | None = None
    services: list[SpaService] | None = None
    staff: list[SpaStaffMember] | None = None
    timezone: str | None = Field(None, max_length=64)
    booking_provider: BookingProvider | None = None
    booking_config: dict[str, Any] | None = None
    twiml_voice: str | None = Field(None, max_length=64)
    voice_engine: VoiceEngine | None = None
    is_active: bool | None = None
    description: str | None = Field(None, max_length=4000)
    public_phone: str | None = Field(None, max_length=32)
    cancellation_policy: str | None = Field(None, max_length=4000)
    amenities: list[str] | None = None
    packages: list[Any] | None = None
    enhancement_settings: EnhancementSettings | None = None
    upsell_rules: list[SpaUpsellRule] | None = None
    payment_policy: SpaPaymentPolicy | None = None
    notification_settings: dict[str, Any] | None = None
    booking_policies: dict[str, Any] | None = None

    @field_validator("enhancement_settings")
    @classmethod
    def validate_enhancement_settings(cls, value):
        if value is None:
            raise ValueError("enhancement_settings cannot be null; use enabled=false to disable")
        return value

    @field_validator("voice_engine")
    @classmethod
    def validate_voice_engine(cls, value: VoiceEngine | None) -> VoiceEngine:
        if value is None:
            raise ValueError("voice_engine cannot be null")
        return value

    @field_validator("twilio_phone_number")
    @classmethod
    def normalize_phone(cls, v: str | None) -> str | None:
        return _normalize_e164(v)

    @field_validator("business_hours", mode="before")
    @classmethod
    def check_hours(cls, v: Any) -> Any:
        return _validate_hours(v) if isinstance(v, dict) else v


class SpaAccountRead(ORMModel, SpaAccountBase):
    id: uuid.UUID
    is_active: bool
    created_at: datetime
    updated_at: datetime
    # Secrets are returned only as masked values by the router.
    booking_config: dict[str, Any] = Field(default_factory=dict)
    booking_provider_configured: bool = False


class SpaAccountSummary(ORMModel):
    """Lightweight projection for the super admin's tenant switcher."""

    id: uuid.UUID
    name: str
    twilio_phone_number: str | None
    booking_provider: BookingProvider
    is_active: bool
