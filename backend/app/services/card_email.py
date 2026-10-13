"""Explicit, tenant-scoped delivery of existing Square card-entry links.

SMTP acceptance is not proof of inbox delivery. Never log message bodies,
addresses, credentials, or raw link tokens. No booking writes occur here.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.headerregistry import Address
import logging
import smtplib
import ssl
import uuid

from email_validator import validate_email, EmailNotValidError

from app.core.config import settings
from app.models.appointment import AppointmentStatus, CardStatus
from app.models.card_entry_token import CardEntryToken
from app.services.card_entry import new_card_token, hash_card_token, card_entry_url, public_square_config
from app.services.booking_config import decrypt_config
from app.services.spa_facts import payment_policy_of

logger = logging.getLogger(__name__)


def email_address(value):
    try:
        return validate_email(value or "", check_deliverability=False).normalized
    except (EmailNotValidError, TypeError):
        return None


def configured_for(spa):
    return bool(
        settings.CARD_EMAIL_TENANT_ID == str(spa.id)
        and settings.CARD_SMTP_HOST
        and email_address(settings.CARD_SMTP_USER)
        and settings.CARD_SMTP_PASSWORD
    )


async def send_card_email(spa, destination, url):
    if not configured_for(spa):
        return "not_configured"

    def send():
        message = EmailMessage()
        name = " ".join((settings.CARD_EMAIL_DISPLAY_NAME or spa.name or "Your spa").split())
        message["From"] = Address(display_name=name, addr_spec=settings.CARD_SMTP_USER)
        message["To"] = destination
        message["Subject"] = "Securely add a card for your appointment"
        message.set_content(
            f"Your appointment with {name} is booked. A card is required on file.\n\n"
            f"Add your card securely using this private link:\n{url}\n\n"
            "No charge is being made now. Please do not email your card details.\n"
            "If the link expires, contact the spa for assistance."
        )
        with smtplib.SMTP_SSL(settings.CARD_SMTP_HOST, settings.CARD_SMTP_PORT,
                              timeout=10, context=ssl.create_default_context()) as client:
            client.login(settings.CARD_SMTP_USER, settings.CARD_SMTP_PASSWORD)
            return "rejected" if client.send_message(message) else "accepted"

    return await asyncio.to_thread(send)


async def offer_secure_card_email(*, store, spa, appointment, contact, link,
                                  adapter, confirmed_email, consent, request_id=None,
                                  sender=send_card_email):
    """Caller/staff must confirm the recipient and consent before invocation."""
    destination = email_address(confirmed_email)
    if not consent or not destination:
        return "email_confirmation_required"
    if not configured_for(spa):
        return "not_configured"
    if (appointment.tenant_id != spa.id or contact is None or link is None
            or contact.tenant_id != spa.id or link.tenant_id != spa.id
            or appointment.contact_id != contact.id or link.contact_id != contact.id
            or link.provider != appointment.booking_provider):
        return "customer_unresolved"
    if destination != email_address(contact.email):
        return "email_mismatch"
    if (appointment.status not in {AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED}
            or not appointment.external_booking_id):
        return "booking_not_confirmed"
    if appointment.card_status != CardStatus.PENDING_CARD:
        return "card_status"
    policy = payment_policy_of(spa)
    if not policy["card_required"] or policy["collection_mode"] != "secure_sms_link":
        return "collection_mode"
    if not getattr(adapter, "supports_save_card_on_file", False) or not link.external_customer_id:
        return "provider_unsupported"
    if not public_square_config(decrypt_config(spa.booking_config)):
        return "square_public_config"
    if not settings.PUBLIC_BASE_URL.startswith("https://"):
        return "secure_url_required"
    now = datetime.now(timezone.utc)
    raw = new_card_token()
    token = CardEntryToken(
        tenant_id=spa.id, appointment_id=appointment.id,
        external_customer_link_id=link.id, token_hash=hash_card_token(raw),
        idempotency_key=f"email:{request_id or uuid.uuid4()}",
        expires_at=now + timedelta(seconds=settings.CARD_ENTRY_TOKEN_TTL_SECONDS),
    )
    # Persist before sending: a delivered message must never reference an
    # uncommitted token. Do not revoke a working SMS link on email failure.
    await store.add(token)
    await store.commit()
    try:
        result = await sender(spa, destination, card_entry_url(raw))
    except Exception:
        logger.warning("Card email delivery outcome unknown appointment=%s", appointment.id)
        return "delivery_unknown"
    if result == "accepted":
        logger.info("Card email accepted by mail server appointment=%s", appointment.id)
        return "accepted"
    token.revoked_at = now
    await store.persist()
    return "rejected"
