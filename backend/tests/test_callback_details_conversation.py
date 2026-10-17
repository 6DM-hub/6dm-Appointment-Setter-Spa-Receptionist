"""After a callback "yes", Cara asks a name and a good time before alerting staff.

Replays the Rubyz test call of 2026-10-17 16:23 UTC: Cara saved the request on
"Yes." with a canned reason, no name and no time, and never asked when to call.
"""
import json
import uuid
from types import SimpleNamespace

import pytest

import app.services.staff_notifications as staff
import app.services.xai_realtime as realtime
from app.services.booking_state import get_draft, save_draft
from app.services.xai_realtime import UNCONNECTED_CALENDAR_CALLBACK_TEXT
from tests.test_early_callback_unconnected_calendar import _prepared
from tests.test_realtime_consultation_order_and_evening import caller_says

ALERT_PHONE = "+15551110001"


def massage_request(call_id="massage"):
    return {
        "type": "response.function_call_arguments.done",
        "name": "check_availability",
        "call_id": call_id,
        "arguments": json.dumps({
            "requested_services": ["Swedish massage"],
            "service_description": "60 minute Swedish massage",
        }),
    }


class World:
    """A fake database and SMS sender shared by one call."""

    def __init__(self, monkeypatch):
        self.rows = {}
        self.sent = []
        self.spa = SimpleNamespace(
            id=uuid.uuid4(),
            name="Rubyz Day Spa",
            twilio_phone_number="+18175550000",
            notification_settings={"booking_escalation": {
                "sms_destinations": [ALERT_PHONE], "email_destinations": [],
                "notifications_enabled": True, "categories": [], "staff_user_ids": [],
            }},
        )
        world = self

        class FakeDb:
            def add(self, row):
                world._pending = row

            async def flush(self):
                row = world._pending
                if getattr(row, "id", None) is None:
                    row.id = uuid.uuid4()
                world.rows[row.id] = row

            async def commit(self):
                return None

            async def get(self, model, key):
                return world.rows.get(key)

        class FakeSessionLocal:
            async def __aenter__(self):
                return FakeDb()

            async def __aexit__(self, *exc):
                return False

        async def fake_prepare(db, session):
            return SimpleNamespace(spa=world.spa)

        async def sender(phone, body):
            world.sent.append((phone, body))
            return "SM1"

        real_notify = staff.notify_staff

        async def notify(spa, event, summary, **kwargs):
            return await real_notify(spa, event, summary, sender=sender)

        monkeypatch.setattr(realtime, "AsyncSessionLocal", FakeSessionLocal)
        monkeypatch.setattr(realtime, "_prepare", fake_prepare)
        monkeypatch.setattr(staff, "notify_staff", notify)


def _spoken(voice):
    return [call.args[0] for call in voice._send_force_message.await_args_list]


async def _offer_callback(voice):
    caller_says(voice, "Yeah, I want to set up an appointment for Tuesday.")
    await voice._handle_function_call(massage_request())
    assert _spoken(voice)[-1] == UNCONNECTED_CALENDAR_CALLBACK_TEXT


@pytest.mark.asyncio
async def test_todays_call_asks_name_then_time_and_alerts_once_with_details(monkeypatch):
    world = World(monkeypatch)
    voice = _prepared(True)
    voice.session.direction = "inbound"
    voice.session.from_number = "+12145550123"
    await _offer_callback(voice)

    caller_says(voice, "Yes.")
    assert await voice._confirm_pending_booking_from_caller("Yes.")
    assert len(world.rows) == 1, "saved immediately so a hang-up cannot lose it"
    assert world.sent == [], "staff alert waits for the details"
    assert _spoken(voice)[-1].endswith("What name should I give them?")

    caller_says(voice, "Dominic.")
    assert await voice._confirm_pending_booking_from_caller("Dominic.")
    assert _spoken(voice)[-1] == "Thanks, Dominic. When's a good time for the team to call you back?"
    assert world.sent == []

    caller_says(voice, "After 3 pm today.")
    assert await voice._confirm_pending_booking_from_caller("After 3 pm today.")
    assert "call you back at the number you're calling from" in _spoken(voice)[-1]

    assert len(world.sent) == 1
    phone, body = world.sent[0]
    assert phone == ALERT_PHONE
    for expected in ("Rubyz Day Spa", "Dominic", "+12145550123", "After 3 pm today",
                     "60 minute Swedish massage", "Tuesday", "Nothing is booked yet."):
        assert expected in body
    assert "repeated availability errors" not in body

    row = next(iter(world.rows.values()))
    assert row.caller_name == "Dominic"
    assert row.preferred_window == "After 3 pm today."
    assert "Swedish massage" in row.reason

    # Call end must not send a second alert.
    await voice._send_pending_callback_alert()
    assert len(world.sent) == 1


@pytest.mark.asyncio
async def test_hang_up_after_yes_still_alerts_staff(monkeypatch):
    world = World(monkeypatch)
    voice = _prepared(True)
    await _offer_callback(voice)
    caller_says(voice, "Yes please.")
    assert await voice._confirm_pending_booking_from_caller("Yes please.")
    assert world.sent == []

    await voice._send_pending_callback_alert()  # what _finalize does at hang-up
    assert len(world.sent) == 1
    assert "Swedish massage" in world.sent[0][1]


@pytest.mark.asyncio
async def test_known_name_skips_straight_to_the_time_question(monkeypatch):
    World(monkeypatch)
    voice = _prepared(True)
    draft = get_draft(voice.session)
    draft.caller_name = "Dominic"
    save_draft(voice.session, draft)
    await _offer_callback(voice)
    caller_says(voice, "Yes.")
    await voice._confirm_pending_booking_from_caller("Yes.")
    assert _spoken(voice)[-1] == (
        "Great, I've saved that for the team, Dominic. When's a good time for them to call you back?"
    )


@pytest.mark.asyncio
async def test_unclear_name_and_any_time_do_not_trap_the_caller(monkeypatch):
    world = World(monkeypatch)
    voice = _prepared(True)
    await _offer_callback(voice)
    for text in ("Yes.", "Why do you need that?", "Whenever works."):
        caller_says(voice, text)
        assert await voice._confirm_pending_booking_from_caller(text)
    assert len(world.sent) == 1
    assert "Best time: any time." in world.sent[0][1]
    assert not voice.session.entities.get("callback_details_pending")


@pytest.mark.asyncio
async def test_card_numbers_in_the_time_answer_are_redacted(monkeypatch):
    world = World(monkeypatch)
    voice = _prepared(True)
    await _offer_callback(voice)
    for text in ("Yes.", "Dominic.", "after 3, my card is 4111111111111111"):
        caller_says(voice, text)
        await voice._confirm_pending_booking_from_caller(text)
    assert "4111" not in world.sent[0][1]
    assert "4111" not in next(iter(world.rows.values())).preferred_window


@pytest.mark.asyncio
async def test_staff_texts_are_sent_from_the_spas_own_number(monkeypatch):
    calls = []

    async def fake_send_sms(self, to_number, body, from_number=None):
        calls.append((to_number, from_number))
        return "SM9"

    from app.services.twilio_service import TwilioService

    monkeypatch.setattr(TwilioService, "send_sms", fake_send_sms)
    spa = SimpleNamespace(
        twilio_phone_number="+18175550000",
        notification_settings={"booking_escalation": {"sms_destinations": [ALERT_PHONE]}},
    )
    result = await staff.notify_staff(spa, "callback_requested", "call back")
    assert result["sms_sent"] == 1
    assert calls == [(ALERT_PHONE, "+18175550000")]


@pytest.mark.asyncio
async def test_failed_booking_alerts_are_sent_from_the_spas_own_number(monkeypatch):
    calls = []

    async def fake_send_sms(to_number, body, from_number=None):
        calls.append(from_number)
        return "SM9"

    from app.services import twilio_service as twilio_module
    from app.services.escalation_mail import send_staff

    monkeypatch.setattr(twilio_module.twilio_service, "send_sms", fake_send_sms)
    spa = SimpleNamespace(
        twilio_phone_number="+18175550000",
        notification_settings={"booking_escalation": {"sms_destinations": [ALERT_PHONE]}},
    )
    result = await send_staff(spa, "booking_failed", "needs attention")
    assert result["deliveries"][0]["status"] == "accepted"
    assert calls == ["+18175550000"]
