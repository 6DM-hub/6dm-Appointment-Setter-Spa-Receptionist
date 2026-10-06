"""Tenant booking configuration validation and protected storage helpers."""
import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.models.spa_account import BookingProvider

SECRET_KEYS = {"api_key", "access_token", "source_password", "client_secret", "refresh_token", "oauth_token"}
CONFIG_KEYS: dict[BookingProvider, tuple[str, ...]] = {
    BookingProvider.GOOGLE_CALENDAR: ("google_calendar_id",),
    BookingProvider.MINDBODY: ("site_id", "api_key", "source_name", "source_password"),
    BookingProvider.MANGOMINT: ("api_key", "location_id"),
    BookingProvider.SQUARE: ("access_token", "location_id"),
    BookingProvider.VAGARO: ("api_key", "business_id"),
    BookingProvider.ZENOTI: ("api_key", "center_id"),
}
# Public values. Missing these must not block booking. application_id is the
# Square Web Payments application id, not a secret.
OPTIONAL_PUBLIC_KEYS: dict[BookingProvider, tuple[str, ...]] = {
    BookingProvider.SQUARE: ("application_id",),
}
MASK = "••••••••"


def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_config(config: dict[str, Any]) -> dict[str, str]:
    if not config:
        return {}
    token = _fernet().encrypt(json.dumps(config, separators=(",", ":")).encode())
    return {"_encrypted": token.decode()}


def decrypt_config(config: dict[str, Any] | None) -> dict[str, Any]:
    config = config or {}
    token = config.get("_encrypted")
    if not isinstance(token, str):
        return config
    try:
        value = json.loads(_fernet().decrypt(token.encode()).decode())
    except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _accepted_keys(provider: BookingProvider) -> set[str]:
    return set(CONFIG_KEYS[provider]) | set(OPTIONAL_PUBLIC_KEYS.get(provider, ()))


def validate_config(provider: BookingProvider, config: dict[str, Any] | None) -> dict[str, Any]:
    values = config or {}
    unknown = set(values) - _accepted_keys(provider)
    if unknown:
        raise ValueError(f"Unsupported {provider.value} booking field(s): {sorted(unknown)}")
    return {key: value.strip() if isinstance(value, str) else value for key, value in values.items() if value not in (None, "")}


def masked_config(provider: BookingProvider, config: dict[str, Any] | None) -> dict[str, Any]:
    values = decrypt_config(config)
    return {
        key: (f"{MASK}{value[-4:]}" if key in SECRET_KEYS and isinstance(value, str) and value else value)
        for key, value in values.items()
        if key in _accepted_keys(provider)
    }


def merge_config(provider: BookingProvider, existing: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    current = decrypt_config(existing)
    incoming = validate_config(provider, incoming)
    for key, value in incoming.items():
        if key in SECRET_KEYS and isinstance(value, str) and value.startswith(MASK):
            continue
        current[key] = value
    return validate_config(provider, current)


def missing_config(provider: BookingProvider, config: dict[str, Any] | None) -> list[str]:
    values = decrypt_config(config)
    if provider is BookingProvider.GOOGLE_CALENDAR:
        return ["google_calendar_id"] if not values.get("google_calendar_id") else []
    return [key for key in CONFIG_KEYS[provider] if not values.get(key)]