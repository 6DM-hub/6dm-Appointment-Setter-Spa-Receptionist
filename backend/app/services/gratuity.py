"""Tenant-scoped gratuity reminder policy and caller-facing copy.

The setting controls speech only.  It never applies a fee to a payment.  A fee
may be spoken only after the business attests that its processor configuration
and applicable law permit that exact policy.
"""
from __future__ import annotations

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any


AFTER_BOOKING_CONFIRMATION = "after_booking_confirmation"
DURING_CLOSING = "during_closing"
DISABLED = "disabled"
VALID_TIMINGS = {AFTER_BOOKING_CONFIRMATION, DURING_CLOSING, DISABLED}

DEFAULT_GRATUITY_POLICY: dict[str, Any] = {
    "enabled": False,
    "cash_preferred": True,
    "card_processing_fee_percent": 4.0,
    "fee_verified": False,
    "timing": AFTER_BOOKING_CONFIRMATION,
    "custom_message": None,
    "use_voice_personality": True,
}

_MANDATORY_LANGUAGE = re.compile(
    r"\b(?:gratuity|gratuities|tips?|tipping)\s+(?:are|is)\s+"
    r"(?:mandatory|required)\b|"
    r"\b(?:mandatory|required)\s+(?:gratuity|gratuities|tips?|tipping)\b|"
    r"\b(?:must|have\s+to|has\s+to|need\s+to|needs\s+to|required\s+to|"
    r"obligated\s+to)\s+(?:tip|leave (?:a )?(?:gratuity|tip))\b|"
    r"\bautomatic(?:ally)?\s+"
    r"(?:(?:added|applied|included|charged)\s+)?(?:gratuity|tip)\b|"
    r"\b(?:included|preincluded)\s+(?:gratuity|tip)\b|"
    r"\b(?:gratuity|gratuities|tips?)\b.{0,40}\b(?:will|shall|is|are)\s+"
    r"(?:be\s+)?(?:automatically\s+)?(?:added|applied|included|charged)\b|"
    r"\b(?:we|the\s+(?:spa|business))\s+(?:will\s+)?"
    r"(?:add|apply|include|charge)\b.{0,30}\b(?:gratuity|tip)\b",
    re.IGNORECASE,
)
_NUMERIC_PERCENT = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:%|per\s*cent(?:age)?\b)",
    re.IGNORECASE,
)
_WORD_PERCENT = re.compile(
    r"\b(zero|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"per\s*cent(?:age)?\b",
    re.IGNORECASE,
)
_PERCENT_MENTION = re.compile(r"%|\bper\s*cent(?:age)?\b", re.IGNORECASE)
_WORD_PERCENT_VALUES = {
    "zero": Decimal("0"),
    "one": Decimal("1"),
    "two": Decimal("2"),
    "three": Decimal("3"),
    "four": Decimal("4"),
    "five": Decimal("5"),
    "six": Decimal("6"),
    "seven": Decimal("7"),
    "eight": Decimal("8"),
    "nine": Decimal("9"),
    "ten": Decimal("10"),
}
_FEE_LANGUAGE = re.compile(
    r"\b(?:fee|fees|surcharge|surcharges|charge|charges|cost|costs)\b",
    re.IGNORECASE,
)
_GRATUITY_TOPIC = re.compile(
    r"\b(?:gratuity|gratuities|tip|tips|tipping)\b|"
    r"\b(?:don'?t|do\s+not|without)\s+(?:have\s+)?cash\b",
    re.IGNORECASE,
)


def _stored_bool(value: Any, *, default: bool = False) -> bool:
    """Read JSON booleans without treating strings such as ``"false"`` as true.

    API writes are validated by Pydantic, but this runtime layer must fail closed
    if old, manually edited, or malformed JSON reaches the database.
    """
    return value if isinstance(value, bool) else default


def _decimal_percent(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not result.is_finite() or result < 0 or result > 4:
        return Decimal("0")
    return result


def format_percent(value: Any) -> str:
    """Render a configured percentage without floating point artifacts."""
    number = _decimal_percent(value).quantize(Decimal("0.01"))
    return format(number.normalize(), "f")


def _claimed_percentages(message: str) -> list[Decimal]:
    """Extract explicit numeric fee claims without clamping them to policy limits."""
    claims: list[Decimal] = []
    for raw in _NUMERIC_PERCENT.findall(message):
        try:
            number = Decimal(raw)
        except InvalidOperation:
            continue
        if number.is_finite() and number >= 0:
            claims.append(number)
    claims.extend(
        _WORD_PERCENT_VALUES[word.casefold()]
        for word in _WORD_PERCENT.findall(message)
    )
    return claims


def normalize_gratuity_policy(value: Any) -> dict[str, Any]:
    raw = dict(value) if isinstance(value, dict) else {}
    policy = {**DEFAULT_GRATUITY_POLICY, **raw}
    timing = str(policy.get("timing") or AFTER_BOOKING_CONFIRMATION).strip().lower()
    if timing not in VALID_TIMINGS:
        timing = DISABLED
    custom = str(policy.get("custom_message") or "").strip() or None
    return {
        "enabled": _stored_bool(policy.get("enabled")),
        "cash_preferred": _stored_bool(policy.get("cash_preferred")),
        "card_processing_fee_percent": float(
            _decimal_percent(policy.get("card_processing_fee_percent"))
        ),
        "fee_verified": _stored_bool(policy.get("fee_verified")),
        "timing": timing,
        "custom_message": custom,
        "use_voice_personality": _stored_bool(
            policy.get("use_voice_personality"), default=True
        ),
    }


def gratuity_policy_of(spa: Any) -> dict[str, Any]:
    payment = getattr(spa, "payment_policy", None)
    if hasattr(payment, "model_dump"):
        payment = payment.model_dump()
    payment = payment if isinstance(payment, dict) else {}
    gratuity = payment.get("gratuity")
    if hasattr(gratuity, "model_dump"):
        gratuity = gratuity.model_dump()
    return normalize_gratuity_policy(gratuity)


def effective(policy: Any) -> bool:
    normalized = normalize_gratuity_policy(policy)
    return bool(normalized["enabled"] and normalized["timing"] != DISABLED)


def fee_may_be_spoken(policy: Any) -> bool:
    normalized = normalize_gratuity_policy(policy)
    return bool(
        normalized["enabled"]
        and normalized["fee_verified"]
        and normalized["card_processing_fee_percent"] > 0
    )


def custom_message_violation(message: str, policy: Any) -> str | None:
    """Return why custom caller-facing copy is unsafe, otherwise ``None``."""
    normalized = normalize_gratuity_policy(policy)
    if _MANDATORY_LANGUAGE.search(message):
        return "mandatory"
    percentages = _claimed_percentages(message)
    has_percent_claim = bool(_PERCENT_MENTION.search(message))
    if (has_percent_claim or _FEE_LANGUAGE.search(message)) and not fee_may_be_spoken(
        normalized
    ):
        return "unverified_fee"
    configured = _decimal_percent(normalized["card_processing_fee_percent"])
    if has_percent_claim and not percentages:
        return "mismatched_fee"
    if any(item != configured for item in percentages):
        return "mismatched_fee"
    return None


def _custom_message_is_safe(message: str, policy: dict[str, Any]) -> bool:
    return custom_message_violation(message, policy) is None


def mentions_gratuity(utterance: str | None) -> bool:
    """Recognize when a caller has already discussed tipping or gratuities."""
    return bool(_GRATUITY_TOPIC.search(utterance or ""))


def booking_card_allows_reminder(card_status: Any) -> bool:
    """Allow speech only after the booking's card state is authoritative."""
    value = getattr(card_status, "value", card_status)
    normalized = str(value or "").strip().lower()
    return normalized in {"not_required", "card_confirmed"}


def _selection_index(selection_key: str, count: int) -> int:
    digest = hashlib.sha256(selection_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % count


def reminder_message(policy: Any, *, selection_key: str) -> str | None:
    """Return one safe reminder, or None when the tenant disabled it.

    Selection is stable for an appointment but varies across bookings.  The
    weighted list keeps the requested roughly 70/30 conversational-to-luxury
    personality without nondeterministic tests or repeats on retries.
    """
    normalized = normalize_gratuity_policy(policy)
    if not effective(normalized):
        return None

    custom = normalized.get("custom_message")
    if custom and _custom_message_is_safe(custom, normalized):
        return custom

    cash = normalized["cash_preferred"]
    fee = fee_may_be_spoken(normalized)
    percent = format_percent(normalized["card_processing_fee_percent"])

    if cash and fee:
        variants = [
            (
                "Just a little reminder before your visit — gratuities are always "
                f"appreciated, and cash is preferred. If you choose to leave a gratuity "
                f"using a card, there is a {percent}% processing fee. We just wanted to "
                "let you know ahead of time so there are no surprises. We look forward "
                "to seeing you!"
            ),
            (
                "Oh, and one quick thing before you come in! If you're planning to leave "
                f"a tip, cash is always appreciated since gratuities left on a card have "
                f"a {percent}% processing fee. Just wanted to give you a heads-up!"
            ),
            (
                "One small courtesy reminder — should you wish to leave a gratuity for "
                f"your service provider, cash is preferred, as card gratuities are subject "
                f"to a {percent}% processing fee. We look forward to welcoming you!"
            ),
            (
                "As a courtesy, please know that cash gratuities are preferred, as card "
                f"gratuities carry a {percent}% processing fee. We look forward to your visit!"
            ),
        ]
    elif cash:
        variants = [
            (
                "Just a little reminder before your visit — gratuities are always "
                "appreciated but never required, and cash is preferred. We look forward "
                "to seeing you!"
            ),
            (
                "One small courtesy reminder — should you wish to leave a gratuity for "
                "your service provider, cash is preferred. We look forward to welcoming you!"
            ),
        ]
    elif fee:
        variants = [
            (
                "Just a courtesy reminder before your visit — gratuities are always "
                f"optional. If you choose to leave one using a card, there is a {percent}% "
                "processing fee. We look forward to seeing you!"
            ),
            (
                "As a courtesy, please know that card gratuities carry a "
                f"{percent}% processing fee. Gratuities are always optional, and we look "
                "forward to your visit!"
            ),
        ]
    else:
        variants = [
            (
                "Just a little reminder before your visit — gratuities are always "
                "appreciated but never required. We look forward to seeing you!"
            )
        ]

    if not normalized["use_voice_personality"]:
        return variants[0]
    # Seven conversational entries and three polished/luxury entries. All four
    # requested scripts remain reachable when the full fee policy is active.
    if len(variants) >= 4:
        weighted = (
            [variants[0]] * 3
            + [variants[1]] * 4
            + [variants[2]] * 2
            + [variants[3]]
        )
    elif len(variants) == 2:
        weighted = [variants[0]] * 7 + [variants[1]] * 3
    else:
        weighted = [variants[0]]
    return weighted[_selection_index(selection_key, len(weighted))]


def gratuity_question_answer(policy: Any) -> str:
    """Answer caller questions without inventing an unverified fee."""
    normalized = normalize_gratuity_policy(policy)
    if not normalized["enabled"]:
        return (
            "Gratuities are optional. This business has not configured a verified "
            "gratuity policy for Cara to quote, so do not invent a cash preference "
            "or card fee."
        )

    parts = ["Gratuities are optional and always at the customer's discretion."]
    if normalized["cash_preferred"]:
        parts.append("Cash gratuities are preferred, but card gratuities are accepted.")
    else:
        parts.append("Card gratuities are accepted.")
    if fee_may_be_spoken(normalized):
        parts.append(
            "The business has verified a "
            f"{format_percent(normalized['card_processing_fee_percent'])}% processing "
            "fee on card gratuities."
        )
    else:
        parts.append("Do not state a card-gratuity processing fee because none is verified.")
    return " ".join(parts)
