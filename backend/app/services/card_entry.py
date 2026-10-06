"""Secure one-time card-on-file links.

The raw token is a credential. It is hashed before storage and is never logged.
Saving a card does not charge it.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.appointment import Appointment, AppointmentStatus, CardStatus
from app.models.card_entry_token import CardEntryToken
from app.models.contact import Contact
from app.models.external_customer_link import ExternalCustomerLink
from app.models.spa_account import SpaAccount
from app.services.booking_adapters.saved_payments import SaveCardOutcome
from app.services.booking_config import decrypt_config
from app.services.card_status import is_guest_booking
from app.services.phone_numbers import canonical_customer_phone
from app.services.spa_facts import payment_policy_of

logger = logging.getLogger(__name__)

SECURE_SMS_MODE = "secure_sms_link"
PUBLIC_REJECTION = "This secure link is invalid or has expired."
CLAIM_STALE_SECONDS = 30

_OPEN_CARD_STATES = {CardStatus.PENDING_CARD, CardStatus.FAILED}


def hash_card_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_card_token() -> str:
    return secrets.token_urlsafe(32)


def card_entry_url(raw_token: str) -> str:
    base = settings.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/card#{raw_token}"


def sms_body(spa_name: str, url: str) -> str:
    name = " ".join((spa_name or "the spa").split()) or "the spa"
    return (
        f"Your appointment with {name} is booked. A card is required to keep on file. "
        f"Add it securely here: {url}. No charge is being made now."
    )


def card_link_block_reason(
    *,
    card_status: CardStatus | None,
    card_required: bool,
    collection_mode: str | None,
    guest_booking: bool,
    can_save_card: bool,
    external_customer_id: str | None,
    link_matches_customer: bool,
    delivery_phone: str | None,
    sender_phone: str | None,
    application_id: str | None,
    location_id: str | None,
) -> str | None:
    """None means a secure link may be issued. Any string is a safe refusal."""
    if card_status is not CardStatus.PENDING_CARD:
        return "card_status"
    if not card_required:
        return "card_not_required"
    if (collection_mode or "").strip().lower() != SECURE_SMS_MODE:
        return "collection_mode"
    if guest_booking:
        return "guest_unresolved"
    if not can_save_card:
        return "provider_unsupported"
    if not (external_customer_id or "").strip() or not link_matches_customer:
        return "customer_unresolved"
    if not delivery_phone:
        return "no_destination"
    if not (sender_phone or "").strip():
        return "no_tenant_sender"
    if not (application_id or "").strip() or not (location_id or "").strip():
        return "square_public_config"
    return None


def public_square_config(config: dict[str, Any]) -> dict[str, str] | None:
    """Values the browser may see. The access token is never included."""
    application_id = str(config.get("application_id") or "").strip()
    location_id = str(config.get("location_id") or "").strip()
    if not application_id or not location_id:
        return None
    environment = str(config.get("environment") or "production").strip().lower()
    if environment != "sandbox":
        environment = "production"
    return {
        "application_id": application_id,
        "location_id": location_id,
        "environment": environment,
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def token_problem(
    token: CardEntryToken,
    appointment: Appointment,
    link: ExternalCustomerLink,
    *,
    now: datetime,
) -> str | None:
    """Why this credential cannot be used. None means it is still open."""
    if token.tenant_id != appointment.tenant_id or token.tenant_id != link.tenant_id:
        return "tenant"
    if token.appointment_id != appointment.id:
        return "appointment"
    if token.external_customer_link_id != link.id:
        return "customer"
    if token.revoked_at is not None:
        return "revoked"
    if token.consumed_at is not None:
        return "consumed"
    if _as_utc(token.expires_at) <= now:
        return "expired"
    if appointment.status == AppointmentStatus.CANCELLED:
        return "cancelled"
    return None


def claim_submission(token: CardEntryToken, now: datetime) -> str:
    """Mark one in-flight save. Returns claimed, busy, or closed."""
    if token.consumed_at is not None or token.revoked_at is not None:
        return "closed"
    if _as_utc(token.expires_at) <= now:
        return "closed"
    started = token.submission_started_at
    if started is not None and (now - _as_utc(started)).total_seconds() < CLAIM_STALE_SECONDS:
        return "busy"
    token.submission_started_at = now
    return "claimed"


@dataclass
class LoadedCardEntry:
    token: CardEntryToken
    appointment: Appointment
    link: ExternalCustomerLink
    spa: SpaAccount
    contact: Contact | None = None


class CardEntryStore(Protocol):
    async def revoke_open(self, appointment_id: uuid.UUID, now: datetime) -> None: ...

    async def add(self, token: CardEntryToken) -> None: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def find_by_hash(self, digest: str) -> LoadedCardEntry | None: ...

    async def claim(self, token: CardEntryToken, now: datetime) -> str: ...

    async def persist(self) -> None: ...


class SqlCardEntryStore:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def revoke_open(self, appointment_id: uuid.UUID, now: datetime) -> None:
        await self.db.execute(
            update(CardEntryToken)
            .where(
                CardEntryToken.appointment_id == appointment_id,
                CardEntryToken.consumed_at.is_(None),
                CardEntryToken.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )

    async def add(self, token: CardEntryToken) -> None:
        self.db.add(token)

    async def commit(self) -> None:
        await self.db.commit()

    async def rollback(self) -> None:
        await self.db.rollback()

    async def find_by_hash(self, digest: str) -> LoadedCardEntry | None:
        token = (
            await self.db.execute(select(CardEntryToken).where(CardEntryToken.token_hash == digest))
        ).scalar_one_or_none()
        if token is None:
            return None
        appointment = await self.db.get(Appointment, token.appointment_id)
        link = await self.db.get(ExternalCustomerLink, token.external_customer_link_id)
        spa = await self.db.get(SpaAccount, token.tenant_id)
        if appointment is None or link is None or spa is None:
            return None
        contact = None
        if appointment.contact_id is not None:
            found = await self.db.get(Contact, appointment.contact_id)
            if found is not None and found.tenant_id == token.tenant_id:
                contact = found
        return LoadedCardEntry(
            token=token,
            appointment=appointment,
            link=link,
            spa=spa,
            contact=contact,
        )

    async def claim(self, token: CardEntryToken, now: datetime) -> str:
        if token.consumed_at is not None or token.revoked_at is not None:
            return "closed"
        if _as_utc(token.expires_at) <= now:
            return "closed"
        result = await self.db.execute(
            update(CardEntryToken)
            .where(
                CardEntryToken.id == token.id,
                CardEntryToken.consumed_at.is_(None),
                CardEntryToken.revoked_at.is_(None),
                CardEntryToken.expires_at > now,
                stale_claim_clause(now),
            )
            .values(submission_started_at=now)
        )
        if result.rowcount != 1:
            await self.db.refresh(token)
            if token.consumed_at is not None:
                return "closed"
            return "busy"
        token.submission_started_at = now
        return "claimed"

    async def persist(self) -> None:
        await self.db.commit()


async def submit_saved_card(
    *,
    store: CardEntryStore,
    loaded: LoadedCardEntry,
    source_id: str,
    verification_token: str | None,
    adapter: Any,
    now: datetime | None = None,
) -> str:
    """Save the browser token onto the customer bound to this link. No charge."""
    now = now or datetime.now(timezone.utc)
    appointment = loaded.appointment
    if appointment.card_status == CardStatus.CARD_CONFIRMED:
        loaded.token.revoked_at = loaded.token.revoked_at or now
        await store.persist()
        return "already_on_file"
    problem = token_problem(loaded.token, appointment, loaded.link, now=now)
    if problem or appointment.card_status not in _OPEN_CARD_STATES:
        return "unavailable"
    claim = await store.claim(loaded.token, now)
    if claim == "busy":
        return "busy"
    if claim != "claimed":
        if appointment.card_status == CardStatus.CARD_CONFIRMED or loaded.token.consumed_at is not None:
            return "already_on_file"
        return "unavailable"
    result = await adapter.save_card_on_file(
        external_customer_id=loaded.link.external_customer_id,
        source_id=source_id,
        idempotency_key=loaded.token.idempotency_key,
        verification_token=verification_token,
    )
    outcome = getattr(result, "outcome", None)
    if outcome == SaveCardOutcome.SAVED:
        loaded.token.consumed_at = now
        loaded.token.submission_started_at = None
        appointment.card_status = CardStatus.CARD_CONFIRMED
        await store.persist()
        return "saved"
    if outcome == SaveCardOutcome.AMBIGUOUS:
        loaded.token.submission_started_at = None
        await store.persist()
        return "ambiguous"
    appointment.card_status = CardStatus.FAILED
    await rotate_idempotency_key(loaded.token)
    await store.persist()
    logger.info(
        "Card save rejected spa=%s code=%s",
        loaded.spa.id,
        getattr(result, "error_code", None),
    )
    return "rejected"


def _delivery_phone(from_number: str | None, contact_phone: str | None, timezone_name: str | None) -> str | None:
    for candidate in (from_number, contact_phone):
        phone = canonical_customer_phone(candidate, timezone_name=timezone_name)
        if phone:
            return phone
    return None


async def offer_secure_card_sms(
    *,
    store: CardEntryStore,
    spa: SpaAccount,
    appointment: Appointment,
    link: ExternalCustomerLink | None,
    adapter: Any,
    caller_name: str | None,
    guest_name: str | None,
    from_number: str | None,
    contact_phone: str | None,
    sender,
) -> str:
    """Issue one active link and text it from this spa's own number.

    Returns a reason code. SMS failure leaves the appointment booked and
    pending. The raw token is not logged.
    """
    policy = payment_policy_of(spa)
    config = decrypt_config(getattr(spa, "booking_config", None))
    public = public_square_config(config) or {}
    customer_id = (link.external_customer_id if link is not None else "") or ""
    reason = card_link_block_reason(
        card_status=appointment.card_status,
        card_required=bool(policy["card_required"]),
        collection_mode=policy["collection_mode"],
        guest_booking=is_guest_booking(caller_name, guest_name),
        can_save_card=bool(getattr(adapter, "supports_save_card_on_file", False)),
        external_customer_id=customer_id,
        link_matches_customer=bool(
            link is not None
            and link.tenant_id == appointment.tenant_id
            and link.provider == (appointment.booking_provider or link.provider)
            and customer_id
        ),
        delivery_phone=_delivery_phone(from_number, contact_phone, getattr(spa, "timezone", None)),
        sender_phone=getattr(spa, "twilio_phone_number", None),
        application_id=public.get("application_id"),
        location_id=public.get("location_id"),
    )
    if reason:
        logger.info("Secure card link not sent: %s", reason)
        return reason
    assert link is not None
    phone = _delivery_phone(from_number, contact_phone, getattr(spa, "timezone", None))
    assert phone
    now = datetime.now(timezone.utc)
    raw = new_card_token()
    await store.revoke_open(appointment.id, now)
    token = CardEntryToken(
        tenant_id=appointment.tenant_id,
        appointment_id=appointment.id,
        external_customer_link_id=link.id,
        token_hash=hash_card_token(raw),
        idempotency_key=str(uuid.uuid4()),
        expires_at=now + timedelta(seconds=settings.CARD_ENTRY_TOKEN_TTL_SECONDS),
    )
    await store.add(token)
    body = sms_body(spa.name, card_entry_url(raw))
    sender_number = str(spa.twilio_phone_number).strip()
    try:
        sid = await sender(phone, body, from_number=sender_number)
    except Exception as exc:
        logger.warning("Secure card SMS failed: %s", type(exc).__name__)
        await store.rollback()
        return "sms_failed"
    if not sid:
        logger.info("Secure card SMS was not accepted")
        await store.rollback()
        return "sms_failed"
    await store.commit()
    logger.info("Secure card link sent for appointment %s", appointment.id)
    return "sent"


def session_state(loaded: LoadedCardEntry, now: datetime) -> str:
    problem = token_problem(loaded.token, loaded.appointment, loaded.link, now=now)
    if loaded.appointment.card_status == CardStatus.CARD_CONFIRMED:
        return "already_on_file"
    if problem:
        return "unavailable"
    if loaded.appointment.card_status not in _OPEN_CARD_STATES:
        return "unavailable"
    if public_square_config(decrypt_config(loaded.spa.booking_config)) is None:
        return "unavailable"
    return "ready"


def billing_contact_fields(contact: Contact | None, tenant_id) -> dict[str, str]:
    """Buyer fields Square may use while storing a card. Empty when unknown."""
    if contact is None or getattr(contact, "tenant_id", None) != tenant_id:
        return {}
    fields = {
        "givenName": str(getattr(contact, "first_name", "") or "").strip(),
        "familyName": str(getattr(contact, "last_name", "") or "").strip(),
        "email": str(getattr(contact, "email", "") or "").strip(),
        "phone": str(getattr(contact, "phone_number", "") or "").strip(),
    }
    return {key: value for key, value in fields.items() if value}


def session_payload(loaded: LoadedCardEntry) -> dict[str, Any]:
    public = public_square_config(decrypt_config(loaded.spa.booking_config)) or {}
    payload: dict[str, Any] = {
        "spa_name": loaded.spa.name,
        "application_id": public.get("application_id", ""),
        "location_id": public.get("location_id", ""),
        "environment": public.get("environment", "production"),
        "state": "ready",
    }
    billing = billing_contact_fields(getattr(loaded, "contact", None), loaded.spa.id)
    if billing:
        payload["billing_contact"] = billing
    return payload


async def rotate_idempotency_key(token: CardEntryToken) -> None:
    token.idempotency_key = str(uuid.uuid4())
    token.submission_started_at = None


def stale_claim_clause(now: datetime):
    cutoff = now - timedelta(seconds=CLAIM_STALE_SECONDS)
    return or_(
        CardEntryToken.submission_started_at.is_(None),
        CardEntryToken.submission_started_at < cutoff,
    )
