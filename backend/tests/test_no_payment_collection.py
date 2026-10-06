"""Phase 1: the test-phase system must not collect payment card details.

No production code currently asks for card data — this test exists to catch
a regression, not to change behavior. It checks both the data model (nothing
a caller says about card details has anywhere to go) and the tool schemas the
realtime model is given (no field or instruction mentions collecting one).

Spoken instructions may name a card number only to forbid collecting one.
A sentence that asks the caller to provide card details still fails.
"""
import json
import re

from app.services.grok_service import AppointmentIntent, build_realtime_instructions
from app.services import xai_realtime

_CARD_WORDS = ("card number", "cvv", "cvc", "expiration date", "card_number", "payment_card")

# Terms a caller must never be asked to speak, type, or key in.
_SENSITIVE_CARD_TERMS = (
    "card number",
    "pan",
    "cvv",
    "cvc",
    "expiration",
    "expiry",
    "security code",
    "payment digits",
    "dtmf",
    "keypad",
)
_PROHIBITION = re.compile(r"\b(never|do not|don't|must not|must never)\b")
_SOLICITATION = re.compile(
    r"\b("
    r"what(?:'s| is) your|"
    r"please (?:provide|give|read|say|enter|tell|speak)|"
    r"tell me your|give me your|read me your|"
    r"enter your|type your|key in|keypad|dtmf"
    r")\b"
)


def test_appointment_intent_has_no_payment_card_fields():
    fields = set(AppointmentIntent.model_fields)
    for word in ("card", "cvv", "cvc", "payment"):
        assert not any(word in f.lower() for f in fields), f"unexpected payment-related field for {word!r}"


def test_realtime_tool_schemas_never_ask_for_card_details():
    tools = [
        xai_realtime.PROPOSE_APPOINTMENT_TOOL,
        xai_realtime.CONFIRM_APPOINTMENT_TOOL,
        xai_realtime.CANCEL_APPOINTMENT_TOOL,
        xai_realtime.NEW_APPOINTMENT_TOOL,
        xai_realtime.MANAGE_APPOINTMENT_TOOL,
        xai_realtime.CHECK_AVAILABILITY_TOOL,
        xai_realtime.LOOKUP_SPA_FACTS_TOOL,
    ]
    blob = json.dumps(tools).lower()
    for word in _CARD_WORDS:
        assert word not in blob


def test_realtime_instructions_never_ask_for_card_details():
    text = build_realtime_instructions(
        "Test Spa", "SERVICE MENU:\n- Facial 60 minutes"
    ).lower()
    assert "never collect full payment card details" in text
    assert "never ask the caller to speak a card number" in text
    assert "never ask the caller to speak full payment card details" in text
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", text)
        if part.strip()
    ]
    for sentence in sentences:
        if not any(term in sentence for term in _SENSITIVE_CARD_TERMS):
            continue
        assert _PROHIBITION.search(sentence), (
            f"card-detail mention is not a prohibition: {sentence}"
        )
        assert not _SOLICITATION.search(sentence), (
            f"instructions ask the caller for card details: {sentence}"
        )
