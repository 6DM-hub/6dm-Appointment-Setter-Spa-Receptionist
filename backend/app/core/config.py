# backend/app/core/config.py
import logging
import socket
import json
from functools import lru_cache
from typing import Literal
from pathlib import Path

from pydantic import AliasChoices, Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def parse_cors_origins(value: str) -> list[str]:
    """Normalize comma-separated origins from local env files and platforms."""
    raw = (value or "").strip()
    if not raw:
        return []
    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                values = parsed
            else:
                values = [raw]
        except json.JSONDecodeError:
            values = [raw]
    else:
        values = raw.split(",")
    origins: list[str] = []
    for item in values:
        origin = str(item).strip().strip('"\'').rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins


@lru_cache
def resolve_service_host(host: str) -> str:
    """Return `host` if it resolves, otherwise fall back to localhost.

    The single shared .env uses Docker Compose service names ("postgres",
    "redis"), which only resolve inside the compose network. When the app is
    run directly on the host (uvicorn in a local venv), those names fail DNS
    lookup and every DB/Redis call raises, surfacing as an opaque HTTP 500.
    Compose publishes both ports to the host, so localhost is the correct
    equivalent there.
    """
    try:
        socket.getaddrinfo(host, None)
        return host
    except OSError:
        logger.warning(
            "Host %r does not resolve; falling back to 'localhost'. "
            "(Expected when running outside Docker Compose.)",
            host,
        )
        return "localhost"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    APP_NAME: str = "6DM Appointment Setter"
    APP_ENV: Literal["local", "development", "staging", "production"] = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"
    
    # Public URL Twilio uses to reach this server (ngrok in dev, production domain in prod)
    PUBLIC_BASE_URL: str = "http://localhost:8000"
    FRONTEND_BASE_URL: str = "http://localhost:5173"

    # --- Google Calendar OAuth ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_OAUTH_REDIRECT_URI: str = "http://localhost:8000/api/v1/spa-accounts/booking/google/callback"
    GOOGLE_OAUTH_STATE_TTL_SECONDS: int = 600

    # --- Database Credentials & Dynamic URLs ---
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "sixdm_db"
    # Railway provides DATABASE_URL; local Docker Compose uses the component
    # variables above. Prefer the complete URL when a platform provides it.
    DATABASE_URL: str | None = None
    
    DB_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    @property
    def postgres_host(self) -> str:
        return resolve_service_host(self.POSTGRES_HOST)

    @property
    def DATABASE_URL_ASYNC(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1).replace(
                "postgresql://", "postgresql+asyncpg://", 1
            )
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.postgres_host}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def DATABASE_URL_SYNC(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL.replace("postgres://", "postgresql+psycopg2://", 1).replace(
                "postgresql://", "postgresql+psycopg2://", 1
            )
        return f"postgresql+psycopg2://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.postgres_host}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    # --- Redis ---
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    RAILWAY_REDIS_URL: str | None = Field(
        default=None,
        validation_alias=AliasChoices("REDIS_URL", "REDIS_CONNECTION_URL"),
    )
    CALL_STATE_TTL_SECONDS: int = 3600  # 1 hour session memory for calls

    @property
    def redis_host(self) -> str:
        return resolve_service_host(self.REDIS_HOST)

    @property
    def REDIS_URL(self) -> str:
        if self.RAILWAY_REDIS_URL:
            return self.RAILWAY_REDIS_URL
        auth = f":{self.REDIS_PASSWORD}@" if self.REDIS_PASSWORD else ""
        return f"redis://{auth}{self.redis_host}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @property
    def redis_uses_connection_url(self) -> bool:
        return bool(self.RAILWAY_REDIS_URL)

    # --- Twilio Telephony ---
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_API_KEY_SID: str = ""
    TWILIO_API_KEY_SECRET: str = ""
    TWILIO_TWIML_APP_SID: str = ""
    TWILIO_PHONE_NUMBER: str = ""  # E.164 format, e.g. +15551234567
    TWILIO_VALIDATE_SIGNATURE: bool = False  # Set to True in Production
    TWILIO_BROWSER_TOKEN_TTL_SECONDS: int = 300
    # Call recording requires a paid Twilio account: trial accounts reject
    # `record` / `recording_status_callback` with "Invalid or disallowed
    # parameters provided". Leave off until the account is upgraded.
    TWILIO_ENABLE_RECORDING: bool = False
    # Twilio Pay <Pay> connector name. Empty means PCI voice capture is not
    # enabled on this account, so the receptionist must not collect card digits.
    TWILIO_PAY_CONNECTOR: str = ""
    # How long a secure card-entry link stays valid. One place only.
    # 60 minutes is long enough to open an SMS and short enough to limit a
    # leaked link. Expired links are not extended in place.
    CARD_ENTRY_TOKEN_TTL_SECONDS: int = 3600
    DEFAULT_TWIML_VOICE: str = "alice"
    DEFAULT_TWIML_LANGUAGE: str = "en-US"

    # Periodic reconciliation of Twilio's call history into `call_logs`.
    # Calls arriving over the SIP trunk never hit our webhooks, so without this
    # they never appear on the dashboard at all. Metadata only — it never writes
    # or overwrites a transcript.
    TWILIO_SYNC_ENABLED: bool = True
    TWILIO_SYNC_INTERVAL_SECONDS: int = 300
    TWILIO_SYNC_LIMIT: int = 50

    # --- Grok / xAI Integration ---
    # Accept either the current xAI names or the older Grok names for compatibility.
    XAI_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("XAI_API_KEY", "GROK_API_KEY"),
    )
    XAI_BASE_URL: str = Field(
        default="https://api.x.ai/v1",
        validation_alias=AliasChoices("XAI_BASE_URL", "GROK_API_BASE_URL"),
    )
    # Keep the voice path on a fast non-reasoning model. Reasoning variants add
    # seconds of dead air and are unsuitable for live calls: measured against
    # this account, grok-4.20-*-non-reasoning answers a receptionist turn in
    # ~1.1s where grok-4.6 takes ~6.0s.
    #
    # The previous default, "grok-2-latest", is retired — the API now answers
    # `Model not found`, so any deployment that did not override it had every
    # Grok call fail and the agent fell back to "I'm having a little trouble
    # right now" on every single turn.
    GROK_MODEL: str = Field(
        default="grok-4.20-0309-non-reasoning",
        validation_alias=AliasChoices("GROK_MODEL", "XAI_MODEL"),
    )
    GROK_TEMPERATURE: float = 0.4
    GROK_MAX_TOKENS: int = 512
    GROK_TIMEOUT_SECONDS: float = 30.0

    # --- xAI Voice Agent (realtime, over SIP) ---
    # A number attached to a Twilio Elastic SIP trunk pointed at sip.voice.x.ai
    # never reaches the <Gather> webhooks in app/api/v1/telephony.py — the trunk
    # overrides the number's voice_url. This block drives that path instead:
    # xAI POSTs `realtime.call.incoming` to /telephony/xai/incoming, we open the
    # realtime WebSocket, speak the greeting, and record the conversation into
    # the same CallLog rows the <Gather> flow writes.
    #
    # Off by default: with it off nothing here runs and the <Gather> flow is
    # untouched. Requires Voice Agent API entitlement on the xAI team.
    XAI_VOICE_ENABLED: bool = False
    # Outbound sales calls use the realtime bridge when this is enabled.
    # Inbound spa routing uses SpaAccount.voice_engine.
    XAI_REALTIME_ENABLED: bool = False
    VOICE_SETUP_ON_STARTUP: bool = False
    VOICE_RELEASE: str = "local"
    SALES_TIMEZONE: str = "America/Chicago"
    # Signing secret returned once when the number is registered with xAI.
    XAI_VOICE_WEBHOOK_SECRET: str = ""
    # Use a documented lowercase built-in ID or an actual custom voice ID.
    # Known legacy aliases (e.g. xai_ara) are normalized before session.update.
    # Historical server behavior accepted unknown names without an error;
    # session.created reports an initial default, not confirmation of our update.
    XAI_VOICE_ID: str = "ara"
    XAI_REALTIME_URL: str = "wss://api.x.ai/v1/realtime"
    # Replay window for webhook-timestamp, per the Standard Webhooks spec.
    XAI_WEBHOOK_TOLERANCE_SECONDS: int = 300
    # Hard ceiling on one voice session, so a wedged WebSocket cannot leak a
    # task and a DB connection for the life of the process.
    XAI_VOICE_MAX_CALL_SECONDS: int = 1800
    
    # --- Auth / JWT ---
    SECRET_KEY: str = Field(default="CHANGE_ME_IN_PRODUCTION_32_CHAR_MIN")
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14

    # --- Scheduling ---
    DEFAULT_APPOINTMENT_DURATION_MINUTES: int = 30
    SALES_PRESENTATION_DURATION_MINUTES: int = 45

    # --- Booking adapters ---
    # Fallback calendar for spa tenants on `booking_provider=google_calendar`
    # that set no per-tenant calendar id in SpaAccount.booking_config.
    GOOGLE_CALENDAR_ID: str = ""
    # Dominic's calendar: the sole destination for 6DM outbound sales bookings.
    SALES_GOOGLE_CALENDAR_ID: str = ""

    @property
    def public_ws_base_url(self) -> str:
        """Translates PUBLIC_BASE_URL into wss:// / ws:// for Twilio Media Streams."""
        return self.PUBLIC_BASE_URL.replace("https://", "wss://").replace("http://", "ws://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
