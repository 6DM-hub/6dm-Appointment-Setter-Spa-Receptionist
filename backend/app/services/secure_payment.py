"""PCI boundary for voice payments.

The realtime assistant, transcripts, logs, Redis, and Postgres may only see
the safe result of a processor. Raw card numbers and CVVs never enter those
systems. Twilio Pay is used only when TWILIO_PAY_CONNECTOR is configured.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.config import settings

_PAN_RE = re.compile(r"(?:\d[ -]?){13,19}")
_CVV_RE = re.compile(r"\b(?:cvv|cvc|cid)\b\s*[:=]?\s*\d{3,4}", re.IGNORECASE)
_FORBIDDEN_KEYS = {
    "pan",
    "card_number",
    "cardnumber",
    "primary_account_number",
    "cvv",
    "cvc",
    "cid",
    "security_code",
    "track",
    "track1",
    "track2",
    "pin",
    "raw_payment",
    "dtmf",
}
_SAFE_KEYS = {
    "status",
    "charged",
    "transaction_id",
    "payment_id",
    "token",
    "masked_card",
    "last4",
    "error",
}


def pci_voice_enabled() -> bool:
    return bool((settings.TWILIO_PAY_CONNECTOR or "").strip())


def contains_sensitive_payment(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in _FORBIDDEN_KEYS:
                return True
            if contains_sensitive_payment(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_sensitive_payment(item) for item in value)
    text = str(value or "")
    return bool(_PAN_RE.search(text) or _CVV_RE.search(text))


def redact_payment_text(text: str) -> str:
    cleaned = _CVV_RE.sub("[redacted]", text or "")
    return _PAN_RE.sub("[redacted]", cleaned)


def safe_processor_result(result: dict[str, Any] | None) -> dict[str, Any]:
    """Keep only processor-safe fields. A charge is successful only when charged is true."""
    if contains_sensitive_payment(result or {}):
        raise ValueError("raw payment data cannot leave the secure processor")
    source = result or {}
    safe = {key: source[key] for key in _SAFE_KEYS if key in source}
    charged = bool(safe.get("charged"))
    status = str(safe.get("status") or ("succeeded" if charged else "received"))
    if status == "succeeded" and not charged:
        status = "received"
    safe["status"] = status
    safe["charged"] = charged
    last4 = str(safe.get("last4") or "")
    if last4 and (len(last4) != 4 or not last4.isdigit()):
        safe.pop("last4", None)
    return safe


def spoken_payment_line(result: dict[str, Any]) -> str:
    safe = safe_processor_result(result)
    if safe["charged"] and safe["status"] == "succeeded":
        return "Perfect, your payment was successful."
    if safe["status"] in {"received", "captured"}:
        return "Perfect, your payment information was received securely."
    return "The payment could not be completed. We can try the secure form again, or use another method the spa has configured."


def secure_collection_plan(policy: dict[str, Any]) -> dict[str, Any]:
    """Describe the handoff. This payload is safe to show the model."""
    mode = str(policy.get("collection_mode") or "none")
    if mode != "secure_voice_card":
        return {"status": "not_required", "collect_raw_card_on_call": False}
    if not pci_voice_enabled():
        return {
            "status": "pci_not_configured",
            "collect_raw_card_on_call": False,
            "pause_recording": True,
            "message": (
                "Secure voice card entry is not enabled on this Twilio account. "
                "Do not ask the caller to speak or type a card number to you. "
                "Say payment cannot be collected on this call until the spa's "
                "secure payment method is available."
            ),
        }
    return {
        "status": "handoff",
        "collect_raw_card_on_call": False,
        "connector_configured": True,
        "pause_recording": True,
        "exclude_from_transcript": True,
        "message": "I'll securely collect your payment details now.",
    }


def pay_twiml(action_url: str) -> str:
    """Twilio Pay markup. It contains the connector name only, never card data."""
    connector = (settings.TWILIO_PAY_CONNECTOR or "").strip()
    if not connector:
        raise ValueError("Twilio Pay connector is not configured")
    safe_action = action_url.replace('"', "")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Pause length=\"1\"/>"
        f'<Pay paymentConnector="{connector}" action="{safe_action}" '
        'maxAttempts="2" timeout="10"/>'
        "</Response>"
    )


def twilio_pay_callback(form: dict[str, Any]) -> dict[str, Any]:
    """Keep Twilio Pay's safe result. Drop CVV, full PAN, and the raw form."""
    lowered = {str(key).lower(): value for key, value in form.items()}
    if any(key in lowered for key in ("securitycode", "cvv", "cvc", "cid")):
        raise ValueError("raw payment data cannot leave the secure processor")
    card = str(lowered.get("paymentcardnumber") or "")
    digits = re.sub(r"\D", "", card)
    if len(digits) > 4:
        raise ValueError("raw payment data cannot leave the secure processor")
    result = str(form.get("Result") or form.get("result") or "").strip().lower()
    charged = result == "success"
    last4 = re.sub(r"\D", "", str(form.get("PaymentCardNumber") or ""))
    last4 = last4[-4:] if len(last4) == 4 else ""
    return safe_processor_result({
        "status": "succeeded" if charged else (result or "failed"),
        "charged": charged,
        "payment_id": str(form.get("PaymentConfirmationCode") or "") or None,
        "token": str(form.get("PaymentToken") or "") or None,
        "last4": last4 or None,
        "error": None if charged else (result or "failed"),
    })


def cache_payment_status(store: dict[str, Any], call_sid: str, result: dict[str, Any]) -> dict[str, Any]:
    """Redis stand-in. Refuses to write anything except the safe result."""
    safe = safe_processor_result(result)
    store[f"payment:{call_sid}"] = safe
    return safe
