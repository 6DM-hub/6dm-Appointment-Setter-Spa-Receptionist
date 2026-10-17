"""Tenant-configured staff alerts, kept separate from customer callbacks.

Destinations come only from spa notification_settings. If none are configured,
the notification is skipped. Two places hold recipients:

- ``booking_escalation``: what the dashboard's "Failed appointment requests"
  settings save. These recipients get the "needs attention" events only
  (callbacks, failed bookings, provider errors), never routine booking news.
- top-level ``sms_destinations`` / ``email_destinations`` / ``events``: the
  older admin-only config. These keep their existing behaviour.

Email goes through the optional SMTP transport; without SMTP settings it is
reported as not configured rather than guessed.
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


# Events that need a person to act. Only these reach dashboard-configured
# (booking_escalation) recipients.
ATTENTION_EVENTS = {"callback_requested", "booking_failed", "provider_error"}


def _settings(spa: Any) -> dict[str, Any]:
    raw = getattr(spa, "notification_settings", None)
    return dict(raw) if isinstance(raw, dict) else {}


def _clean(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(item).strip() for item in values if str(item).strip()]


def notification_plan(spa: Any, event: str, summary: str) -> dict[str, Any]:
    summary = redact_payment_text(summary or "")
    config = _settings(spa)
    phones: list[str] = []
    emails: list[str] = []
    skip_reason = "no_destination"

    enabled = config.get("events")
    if isinstance(enabled, list) and enabled and event not in enabled:
        skip_reason = "event_not_enabled"
    else:
        phones += _clean(config.get("sms_destinations"))
        emails += _clean(config.get("email_destinations"))

    escalation = config.get("booking_escalation")
    if isinstance(escalation, dict) and event in ATTENTION_EVENTS:
        if escalation.get("notifications_enabled", True) is False:
            if not phones and not emails:
                skip_reason = "disabled"
        else:
            phones += _clean(escalation.get("sms_destinations"))
            emails += _clean(escalation.get("email_destinations"))

    phones = list(dict.fromkeys(phones))
    emails = list(dict.fromkeys(emails))
    if not phones and not emails:
        return {"status": "skipped", "reason": skip_reason, "summary": summary}
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
    email_sender: Any = None,
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
            try:
                sid = await sender(phone, plan["summary"])
            except Exception:
                # One bad number must not stop the other recipients.
                logger.exception("staff SMS failed for event %s", event)
                continue
            if sid:
                sent.append({"channel": "sms"})
    emails_sent = 0
    email_status = "none"
    if plan["email_destinations"]:
        if email_sender is None:
            from app.services.escalation_mail import send_email

            email_sender = send_email
        results = []
        for address in plan["email_destinations"]:
            try:
                results.append(await email_sender(address, plan["summary"]))
            except Exception:
                logger.exception("staff email failed for event %s", event)
                results.append("delivery_unknown")
        emails_sent = results.count("accepted")
        if emails_sent:
            email_status = "sent"
        elif len(set(results)) == 1:
            email_status = results[0]
        else:
            email_status = "failed_or_unknown"
    status = "sent" if sent or emails_sent else "skipped"
    return {
        "status": status,
        "event": event,
        "sms_sent": len(sent),
        "email": email_status,
        "emails_sent": emails_sent,
    }


def callback_staff_summary(payload: dict[str, Any], business_name: str | None = None) -> str:
    """Staff text for a saved callback request: enough to call the person back."""
    parts = [f"Cara: callback requested{' for ' + business_name if business_name else ''}."]
    if payload.get("caller_name"):
        parts.append(f"Name: {payload['caller_name']}.")
    parts.append(f"Phone: {payload.get('caller_phone') or 'not available, check the call log'}.")
    if payload.get("reason"):
        parts.append(f"About: {payload['reason']}.")
    if payload.get("preferred_window"):
        parts.append(f"Best time: {payload['preferred_window']}.")
    parts.append("Nothing is booked yet.")
    return redact_payment_text(" ".join(parts))[:600]


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
