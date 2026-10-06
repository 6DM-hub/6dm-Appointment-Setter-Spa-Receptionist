"""Tenant-configured staff alerts, kept separate from customer callbacks.

Destinations come only from spa notification_settings. If none are configured,
the notification is skipped. There is no email transport in this app, so
configured email addresses are reported as not sent rather than guessed.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.secure_payment import redact_payment_text

logger = logging.getLogger(__name__)

STAFF_EVENTS = {
    "booking_created",
    "cancelled",
    "rescheduled",
    "callback_requested",
    "booking_failed",
    "provider_error",
}


def _settings(spa: Any) -> dict[str, Any]:
    raw = getattr(spa, "notification_settings", None)
    return dict(raw) if isinstance(raw, dict) else {}


def notification_plan(spa: Any, event: str, summary: str) -> dict[str, Any]:
    summary = redact_payment_text(summary or "")
    config = _settings(spa)
    enabled = config.get("events")
    if isinstance(enabled, list) and enabled and event not in enabled:
        return {"status": "skipped", "reason": "event_not_enabled", "summary": summary}
    phones = [
        str(item).strip()
        for item in (config.get("sms_destinations") or [])
        if str(item).strip()
    ]
    emails = [
        str(item).strip()
        for item in (config.get("email_destinations") or [])
        if str(item).strip()
    ]
    if not phones and not emails:
        return {"status": "skipped", "reason": "no_destination", "summary": summary}
    return {
        "status": "ready",
        "event": event,
        "summary": summary,
        "sms_destinations": phones,
        "email_destinations": emails,
    }


async def notify_staff(
    spa: Any,
    event: str,
    summary: str,
    *,
    sender: Any = None,
) -> dict[str, Any]:
    plan = notification_plan(spa, event, summary)
    if plan["status"] != "ready":
        logger.info("staff notification skipped: %s", plan.get("reason"))
        return plan
    sent: list[dict[str, str]] = []
    if plan["sms_destinations"]:
        if sender is None:
            from app.services.twilio_service import TwilioService

            service = TwilioService()
            sender = service.send_sms
        for phone in plan["sms_destinations"]:
            sid = await sender(phone, plan["summary"])
            if sid:
                sent.append({"channel": "sms"})
    email_status = "not_sent_no_mailer" if plan["email_destinations"] else "none"
    status = "sent" if sent else "skipped"
    return {
        "status": status,
        "event": event,
        "sms_sent": len(sent),
        "email": email_status,
    }


def callback_request_payload(
    *,
    spa_id: Any,
    call_sid: str,
    caller_phone: str | None,
    caller_name: str | None,
    reason: str | None,
    preferred_window: str | None,
) -> dict[str, Any]:
    """A customer follow-up. This is not a staff notification."""
    return {
        "kind": "callback",
        "spa_id": str(spa_id) if spa_id else None,
        "call_sid": call_sid,
        "caller_phone": (caller_phone or "").strip() or None,
        "caller_name": redact_payment_text(caller_name or "").strip() or None,
        "reason": redact_payment_text(reason or "").strip() or None,
        "preferred_window": redact_payment_text(preferred_window or "").strip() or None,
        "status": "open",
    }
