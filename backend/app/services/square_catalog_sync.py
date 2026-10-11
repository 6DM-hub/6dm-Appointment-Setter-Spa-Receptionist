"""Read-only Square catalog discovery and stale-safe service synchronization.

The preview token is a short-lived, tenant-bound HMAC over both the Square
catalog snapshot and Cara's normalized service rows. Applying a preview always
re-reads Square and the database, so an owner can never approve one set of
changes and accidentally apply a newer one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from app.core.config import settings
from app.models import Service, SpaAccount
from app.services.booking_adapters.providers.square import SquareAdapter


PREVIEW_TTL_SECONDS = 15 * 60
_GENERIC_VARIATION = re.compile(
    r"^(?:regular|standard|default|appointment|service|\d+\s*(?:min(?:ute)?s?|hr|hours?))$",
    re.IGNORECASE,
)


class StaleCatalogPreview(ValueError):
    """The approved preview no longer describes current provider/local data."""


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean(value).casefold()).strip()


def _price_string(amount: int | None) -> str | None:
    if amount is None:
        return None
    value = Decimal(amount) / Decimal(100)
    return format(value.quantize(Decimal("0.01")), "f").rstrip("0").rstrip(".")


def _display_name(item_name: str, variation_name: str, duration_minutes: int) -> str:
    name = item_name or variation_name or "Square service"
    variation = variation_name.strip()
    if variation and _normalized(variation) != _normalized(name) and not _GENERIC_VARIATION.fullmatch(variation):
        name = f"{name} - {variation}"
    duration_phrase = re.compile(rf"\b{duration_minutes}\s*(?:min(?:ute)?s?)\b", re.I)
    if duration_minutes and not duration_phrase.search(name):
        name = f"{name} ({duration_minutes} min)"
    return name[:255]


def _category_name(item_data: dict[str, Any], service_name: str) -> str | None:
    # Square's service search response provides a category id but generally not
    # its display name. Prefer an explicit expanded name when present, then use
    # a conservative spa category suggestion for the owner's review.
    explicit = _clean(item_data.get("category_name"))
    if explicit:
        return explicit[:255]
    normalized = _normalized(service_name)
    for token, category in (
        ("facial", "Facial"),
        ("massage", "Massage"),
        ("wax", "Waxing"),
        ("package", "Spa Packages"),
        ("wellness", "Wellness Treatment"),
    ):
        if token in normalized:
            return category
    return "Wellness Treatment"


def normalize_square_catalog(objects: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten bookable Square appointment variations into stable public data."""
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in objects:
        if not isinstance(item, dict) or item.get("type") != "ITEM":
            continue
        item_data = item.get("item_data") or {}
        if item.get("is_deleted") or item_data.get("is_archived"):
            continue
        if item_data.get("product_type") != "APPOINTMENTS_SERVICE":
            continue
        item_name = _clean(item_data.get("name"))
        description = (_clean(item_data.get("description")) or None)
        if description:
            description = description[:1000]
        for variation in item_data.get("variations") or []:
            if not isinstance(variation, dict) or variation.get("type") != "ITEM_VARIATION":
                continue
            data = variation.get("item_variation_data") or {}
            variation_id = _clean(variation.get("id"))
            if not variation_id or variation_id in seen or variation.get("is_deleted"):
                continue
            if data.get("available_for_booking") is not True:
                continue
            duration_ms = data.get("service_duration")
            try:
                duration = int(duration_ms) // 60_000
            except (TypeError, ValueError):
                duration = 0
            if duration < 5:
                # Duration is mandatory for safe appointment scheduling.
                continue
            money = data.get("price_money") or {}
            try:
                amount = int(money["amount"]) if money.get("amount") is not None else None
            except (TypeError, ValueError):
                amount = None
            version = variation.get("version")
            try:
                version = int(version) if version is not None else None
            except (TypeError, ValueError):
                version = None
            variation_name = _clean(data.get("name"))
            rows.append(
                {
                    "square_item_id": _clean(item.get("id")),
                    "square_variation_id": variation_id,
                    "square_version": version,
                    "item_name": item_name,
                    "variation_name": variation_name,
                    "name": _display_name(item_name, variation_name, duration),
                    "category_name": _category_name(item_data, item_name),
                    "description": description,
                    "duration_minutes": duration,
                    "price": _price_string(amount),
                    "price_amount": amount,
                    "price_currency": _clean(money.get("currency")) or None,
                }
            )
            seen.add(variation_id)
    return sorted(rows, key=lambda row: (_normalized(row["name"]), row["square_variation_id"]))


async def fetch_square_catalog(
    spa: SpaAccount, config: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    """Read every non-archived, bookable service for the configured location."""
    adapter = SquareAdapter(spa.name, config)
    location = await adapter.describe_location()
    location_name = _clean((location or {}).get("name")) or None
    objects: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(25):
        payload: dict[str, Any] = {
            "limit": 100,
            "product_types": ["APPOINTMENTS_SERVICE"],
            "enabled_location_ids": [adapter.location_id],
            "archived_state": "ARCHIVED_STATE_NOT_ARCHIVED",
        }
        if cursor:
            payload["cursor"] = cursor
        response = await adapter._request("POST", "/v2/catalog/search-catalog-items", json=payload)
        objects.extend(item for item in response.get("items") or [] if isinstance(item, dict))
        next_cursor = _clean(response.get("cursor"))
        if not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise ValueError("Square catalog returned a repeated cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    else:
        raise ValueError("Square catalog pagination exceeded its safe limit")
    return normalize_square_catalog(objects), location_name


def catalog_fingerprint(rows: Iterable[dict[str, Any]]) -> str:
    public = [
        {
            key: row.get(key)
            for key in (
                "square_item_id",
                "square_variation_id",
                "square_version",
                "name",
                "description",
                "category_name",
                "duration_minutes",
                "price",
                "price_amount",
                "price_currency",
            )
        }
        for row in rows
    ]
    encoded = json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def local_fingerprint(services: Iterable[Service]) -> str:
    rows = sorted(
        (
            {
                "id": str(service.id),
                "category_id": str(service.category_id),
                "name": service.name,
                "description": service.description,
                "price": service.price,
                "duration_minutes": service.duration_minutes,
                "is_active": service.is_active,
                "square_item_id": service.square_item_id,
                "square_variation_id": service.square_variation_id,
                "square_variation_version": service.square_variation_version,
                "voice_metadata": service.voice_metadata or {},
            }
            for service in services
        ),
        key=lambda row: row["id"],
    )
    encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def provider_identity_fingerprint(config: dict[str, Any]) -> str:
    """Hash the non-secret Square account context used by the preview.

    A catalog can be byte-for-byte identical at two Square locations or across
    sandbox and production. Binding approval to this hash prevents an owner
    from applying a preview after those settings change without putting either
    identifier in the public response or token.
    """
    identity = {
        "environment": _clean(
            config.get("environment")
            or getattr(settings, "SQUARE_ENVIRONMENT", "production")
        ).casefold(),
        "location_id": _clean(config.get("location_id")),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def make_preview_token(
    spa_id: uuid.UUID,
    catalog_hash: str,
    local_hash: str,
    provider_hash: str,
    *,
    issued_at: int | None = None,
) -> str:
    payload = {
        "v": 1,
        "spa": str(spa_id),
        "catalog": catalog_hash,
        "local": local_hash,
        "provider": provider_hash,
        "iat": int(time.time() if issued_at is None else issued_at),
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).rstrip(b"=")
    signature = hmac.new(
        settings.SECRET_KEY.encode(), b"square-catalog-preview:" + body, hashlib.sha256
    ).digest()
    return f"{body.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def verify_preview_token(
    token: str,
    spa_id: uuid.UUID,
    catalog_hash: str,
    local_hash: str,
    provider_hash: str,
    *,
    now: int | None = None,
) -> None:
    try:
        encoded_body, encoded_signature = token.split(".", 1)
        body = encoded_body.encode()
        signature = base64.urlsafe_b64decode(encoded_signature + "=" * (-len(encoded_signature) % 4))
        expected = hmac.new(
            settings.SECRET_KEY.encode(), b"square-catalog-preview:" + body, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            raise StaleCatalogPreview("Preview token is invalid")
        payload = json.loads(
            base64.urlsafe_b64decode(encoded_body + "=" * (-len(encoded_body) % 4)).decode()
        )
    except StaleCatalogPreview:
        raise
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StaleCatalogPreview("Preview token is invalid") from exc

    timestamp = int(time.time() if now is None else now)
    try:
        issued_at = int(payload["iat"])
    except (KeyError, TypeError, ValueError) as exc:
        raise StaleCatalogPreview("Preview token is invalid") from exc
    if issued_at > timestamp + 60 or timestamp - issued_at > PREVIEW_TTL_SECONDS:
        raise StaleCatalogPreview("Preview expired; refresh the Square catalog")
    if payload.get("v") != 1 or payload.get("spa") != str(spa_id):
        raise StaleCatalogPreview("Preview belongs to another business")
    if not hmac.compare_digest(str(payload.get("catalog") or ""), catalog_hash):
        raise StaleCatalogPreview("Square catalog changed; refresh the preview")
    if not hmac.compare_digest(str(payload.get("local") or ""), local_hash):
        raise StaleCatalogPreview("Cara services changed; refresh the preview")
    if not hmac.compare_digest(str(payload.get("provider") or ""), provider_hash):
        raise StaleCatalogPreview("Square connection changed; refresh the preview")


def _service_value(service: Service, field: str) -> Any:
    return getattr(service, field, None)


def build_preview(catalog: list[dict[str, Any]], services: list[Service]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Compare a provider snapshot with one tenant's normalized service rows."""
    by_variation: dict[str, list[Service]] = {}
    by_name_duration: dict[tuple[str, int], list[Service]] = {}
    by_name: dict[str, list[Service]] = {}
    for service in services:
        if service.square_variation_id:
            by_variation.setdefault(service.square_variation_id, []).append(service)
        name = _normalized(service.name)
        by_name_duration.setdefault((name, service.duration_minutes), []).append(service)
        by_name.setdefault(name, []).append(service)

    summary = {"additions": 0, "updates": 0, "conflicts": 0, "unchanged": 0}
    result: list[dict[str, Any]] = []
    for remote in catalog:
        matches = by_variation.get(remote["square_variation_id"], [])
        matched_by = "square_variation_id"
        conflicts: list[str] = []
        if not matches:
            candidate_names = {
                _normalized(remote["name"]),
                _normalized(remote.get("item_name")),
                _normalized(f"{remote['duration_minutes']} minute {remote.get('item_name') or ''}"),
                _normalized(f"{remote.get('item_name') or ''} {remote.get('variation_name') or ''}"),
            }
            candidate_names.discard("")
            unique_matches: dict[str, Service] = {}
            for candidate_name in candidate_names:
                for candidate in by_name_duration.get(
                    (candidate_name, remote["duration_minutes"]), []
                ):
                    unique_matches[str(candidate.id)] = candidate
            matches = list(unique_matches.values())
            matched_by = "name_and_duration"
        if len(matches) > 1:
            conflicts.append("multiple_local_services_match")
        match = matches[0] if len(matches) == 1 else None
        if match and matched_by == "name_and_duration" and match.square_variation_id not in (None, remote["square_variation_id"]):
            conflicts.append("local_service_linked_to_another_square_variation")
        if match and not match.is_active:
            conflicts.append("matching_cara_service_is_inactive")
        if match is None and not conflicts and by_name.get(_normalized(remote["name"])):
            conflicts.append("same_name_has_different_duration")

        if conflicts:
            action = "conflict"
        elif match is None:
            action = "add"
            changes = (
                [{"field": "description", "current": None, "square": remote["description"]}]
                if remote.get("description")
                else []
            )
        else:
            comparisons = {
                "name": (_service_value(match, "name"), remote["name"]),
                "duration_minutes": (_service_value(match, "duration_minutes"), remote["duration_minutes"]),
                "price": (_service_value(match, "price"), remote["price"]),
                "square_item_id": (_service_value(match, "square_item_id"), remote["square_item_id"]),
                "square_variation_id": (_service_value(match, "square_variation_id"), remote["square_variation_id"]),
                "square_version": (_service_value(match, "square_variation_version"), remote["square_version"]),
            }
            if not _service_value(match, "description") and remote.get("description"):
                comparisons["description"] = (
                    _service_value(match, "description"),
                    remote["description"],
                )
            changes = [
                {"field": field, "current": before, "square": after}
                for field, (before, after) in comparisons.items()
                if before != after
            ]
            action = "update" if changes else "unchanged"
        if conflicts:
            changes = []
        summary[{"add": "additions", "update": "updates", "conflict": "conflicts", "unchanged": "unchanged"}[action]] += 1
        result.append(
            {
                "square_item_id": remote["square_item_id"],
                "square_variation_id": remote["square_variation_id"],
                "square_version": remote["square_version"],
                "name": remote["name"],
                "category_name": remote.get("category_name"),
                "duration_minutes": remote["duration_minutes"],
                "price": remote["price"],
                "description": remote.get("description"),
                "action": action,
                "existing_service_id": str(match.id) if match else None,
                "changes": changes,
                "conflict_reason": ", ".join(conflicts) if conflicts else None,
            }
        )
    return result, summary


def generated_at() -> str:
    return datetime.now(timezone.utc).isoformat()

