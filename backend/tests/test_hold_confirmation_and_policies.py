"""Hold tone, booking confirmation copy, staff alerts, VIP, and the PCI boundary."""
import asyncio
import logging

import pytest

from app.models.follow_up_request import FollowUpRequest
from app.services.hold_tone import soft_hold_chunk
from app.services.secure_payment import (
    cache_payment_status,
    contains_sensitive_payment,
    pci_voice_enabled,
    safe_processor_result,
    secure_collection_plan,
    spoken_payment_line,
)
from app.services.spa_facts import configured_vip, policy_statement
from app.services.staff_notifications import callback_request_payload, notification_plan, notify_staff
from app.services.xai_realtime import HOLD_ACK_TEXT, HOLD_ACK_TEXTS, XAIVoiceSession, _caller_is_finished
from app.services.call_state import CallSession
from app.services.booking_state import get_draft, save_draft


def _session() -> CallSession:
    return CallSession(
        call_sid="CAhold",
        direction="inbound",
        from_number="+15551212",
        to_number="+15550001",
        business_name="Test Spa",
    )


def test_hold_chunk_is_short():
    chunk = soft_hold_chunk()
    assert 0 < len(chunk) <= 8000 * 3


def test_fast_cancel_does_not_start_hold():
    voice = XAIVoiceSession("CAhold", _session())
    voice.HOLD_ACK_DELAY_SECONDS = 30
    started = []

    async def start():
        started.append(1)

    voice.start_hold_tone = start

    async def run():
        task = asyncio.create_task(voice._maybe_hold_ack())
        await asyncio.sleep(0)
        task.cancel()
        await task

    asyncio.run(run())
    assert started == []


def test_slow_lookup_acks_then_starts_one_hold():
    voice = XAIVoiceSession("CAhold", _session())
    voice.HOLD_ACK_DELAY_SECONDS = 0
    voice.HOLD_TONE_DELAY_SECONDS = 0
    voice._ws = object()
    spoken = []
    started = []

    async def speak(message, **_kwargs):
        spoken.append(message)

    async def start():
        started.append(1)

    voice._send_force_message = speak
    voice.start_hold_tone = start

    async def run():
        await voice._maybe_hold_ack()
        await voice._maybe_hold_ack()

    asyncio.run(run())
    assert spoken == [HOLD_ACK_TEXT]
    assert started == [1]


def test_lookup_status_rotates_without_thinking_filler():
    voice = XAIVoiceSession("CAhold", _session())
    voice.HOLD_ACK_DELAY_SECONDS = 0
    voice.HOLD_TONE_DELAY_SECONDS = 0
    voice._ws = object()
    spoken = []

    async def speak(message, **_kwargs):
        spoken.append(message)

    voice._send_force_message = speak

    async def run():
        for _ in range(3):
            voice._hold_ack_played_this_turn = False
            await voice._maybe_hold_ack()

    asyncio.run(run())
    assert spoken == list(HOLD_ACK_TEXTS)
    assert all("hmm" not in line.lower() for line in spoken)


def test_arming_availability_does_not_speak_before_five_second_timer():
    voice = XAIVoiceSession("CAhold", _session())
    voice._ws = object()
    spoken = []

    async def speak(message, **_kwargs):
        spoken.append(message)

    voice._send_force_message = speak
    asyncio.run(voice._arm_availability_hold())
    assert spoken == []


def test_failed_and_cancelled_tool_stops_hold():
    voice = XAIVoiceSession("CAhold", _session())
    voice.HOLD_ACK_DELAY_SECONDS = 30
    stopped = []

    async def stop():
        stopped.append(1)

    voice.stop_hold_tone = stop

    async def boom(_args):
        raise RuntimeError("calendar down")

    async def run():
        with pytest.raises(RuntimeError):
            await voice._invoke_tool_with_hold("check_availability", boom, "{}")

    asyncio.run(run())
    assert stopped == [1]


def test_confirmation_includes_details_and_follow_up_question():
    session = _session()
    session.selected_service = "Facial"
    session.confirmed_datetime = "2026-10-02T22:00:00+00:00"
    session.timezone = "America/Los_Angeles"
    draft = get_draft(session)
    draft.preferred_staff = "SIX"
    save_draft(session, draft)
    voice = XAIVoiceSession("CAhold", session)
    text = voice._authoritative_tool_followup(
        "confirm_appointment",
        '{"status":"booked","appointment_id":"a","external_booking_id":"b"}',
    )
    assert "Facial" in text
    assert "SIX" in text
    assert "Is there anything else I can help you with today?" in text
    assert "booked" not in text.lower() or "confirmed" in text


def test_wrap_up_only_for_a_bare_no():
    assert _caller_is_finished("No thanks")
    assert _caller_is_finished("That's all")
    assert not _caller_is_finished("No, book me Thursday at 4")
    assert not _caller_is_finished("yes")


def test_staff_notification_skips_when_unconfigured():
    spa = type("Spa", (), {"notification_settings": {}})()
    plan = notification_plan(spa, "booking_created", "booked")
    assert plan["status"] == "skipped"
    assert plan["reason"] == "no_destination"


def test_staff_notification_uses_only_configured_phones():
    spa = type("Spa", (), {
        "notification_settings": {"sms_destinations": ["+15551110000"], "events": ["booking_created"]},
    })()

    async def sender(phone, body):
        assert phone == "+15551110000"
        assert "4111" not in body
        return "SM123"

    result = asyncio.run(notify_staff(
        spa,
        "booking_created",
        "New booking. card 4111111111111111",
        sender=sender,
    ))
    assert result["status"] == "sent"
    assert result["sms_sent"] == 1


def test_callback_is_not_a_staff_alert_and_redacts_cards():
    payload = callback_request_payload(
        spa_id="spa",
        call_sid="CA1",
        caller_phone="+15550001",
        caller_name="Ada",
        reason="please call me, card 4111111111111111 cvv: 123",
        preferred_window="tomorrow afternoon",
    )
    assert payload["kind"] == "callback"
    assert "4111" not in payload["reason"]
    assert "123" not in payload["reason"]
    columns = {column.name for column in FollowUpRequest.__table__.columns}
    assert "pan" not in columns
    assert "cvv" not in columns


def test_ai_never_receives_pan_or_cvv():
    assert contains_sensitive_payment({"card_number": "4111111111111111"})
    assert contains_sensitive_payment("cvv: 999")
    with pytest.raises(ValueError):
        safe_processor_result({"status": "succeeded", "charged": True, "pan": "4111111111111111"})
    safe = safe_processor_result({
        "status": "succeeded",
        "charged": True,
        "transaction_id": "txn_1",
        "last4": "4242",
        "masked_card": "****4242",
    })
    assert safe["last4"] == "4242"
    assert "pan" not in safe
    assert spoken_payment_line(safe) == "Perfect, your payment was successful."
    received = safe_processor_result({"status": "succeeded", "charged": False, "payment_id": "pay_1"})
    assert received["status"] == "received"
    assert spoken_payment_line(received) == "Perfect, your payment information was received securely."


def test_payment_failure_and_cancel_do_not_claim_success():
    failed = safe_processor_result({"status": "failed", "charged": False, "error": "declined"})
    assert "successful" not in spoken_payment_line(failed)
    cancelled = safe_processor_result({"status": "cancelled", "charged": False})
    assert spoken_payment_line(cancelled).startswith("The payment could not")


def test_raw_payment_is_not_cached(caplog):
    store = {}
    with pytest.raises(ValueError):
        cache_payment_status(store, "CA1", {"cvv": "123", "status": "ok"})
    assert store == {}
    cache_payment_status(store, "CA1", {"status": "received", "charged": False, "last4": "4242"})
    blob = str(store)
    assert "cvv" not in blob
    with caplog.at_level(logging.INFO):
        logging.getLogger("app.services.secure_payment").info("status %s", store["payment:CA1"])
    assert "4111" not in caplog.text


def test_secure_voice_requires_connector(monkeypatch):
    monkeypatch.setattr("app.services.secure_payment.settings.TWILIO_PAY_CONNECTOR", "")
    assert pci_voice_enabled() is False
    plan = secure_collection_plan({"collection_mode": "secure_voice_card", "card_required": True})
    assert plan["status"] == "pci_not_configured"
    assert plan["collect_raw_card_on_call"] is False
    assert "card number" in plan["message"]
    monkeypatch.setattr("app.services.secure_payment.settings.TWILIO_PAY_CONNECTOR", "spa_pay")
    ready = secure_collection_plan({"collection_mode": "secure_voice_card"})
    assert ready["status"] == "handoff"
    assert ready["message"] == "I'll securely collect your payment details now."
    assert "connector_configured" in ready
    from app.services.secure_payment import pay_twiml, twilio_pay_callback

    xml = pay_twiml("https://example.test/pay")
    assert "<Pay " in xml
    assert "4111" not in xml
    safe = twilio_pay_callback({
        "Result": "success",
        "PaymentConfirmationCode": "pay_9",
        "PaymentCardNumber": "4242",
    })
    assert safe["charged"] is True
    assert safe["last4"] == "4242"
    with pytest.raises(ValueError):
        twilio_pay_callback({
            "Result": "success",
            "PaymentCardNumber": "4111111111111111",
            "SecurityCode": "123",
        })


def test_no_vip_config_creates_no_vip_behavior():
    result = configured_vip([])
    assert result["status"] == "unknown"
    assert result["verified"] is False


def test_vip_returns_only_configured_fields():
    packages = [{
        "vip": True,
        "name": "Gold",
        "vip_identifier": "GOLD",
        "eligible_services": ["Facial"],
        "eligible_staff": ["Alex"],
    }]
    matched = configured_vip(packages, "GOLD")
    assert matched["verified"] is True
    assert matched["packages"][0]["eligible_staff"] == ["Alex"]
    assert "credits" not in matched["packages"][0]
    unknown = configured_vip(packages, "SIX")
    assert unknown["status"] == "unverified"


def test_missing_policy_is_unknown_and_informational():
    missing = policy_statement({"booking_policies": {}, "cancellation_policy": None})
    assert missing["status"] == "unknown"
    assert missing["informational_only"] is True
    present = policy_statement({
        "booking_policies": {"cancellation_cutoff": "24 hours"},
        "cancellation_policy": "Cancel 24 hours ahead.",
    })
    assert present["configured"] == {"cancellation_cutoff": "24 hours"}
    assert "fee" not in present["configured"]
