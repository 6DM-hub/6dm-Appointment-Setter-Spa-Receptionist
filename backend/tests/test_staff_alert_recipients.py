"""Staff alerts reach the recipients saved on the dashboard's escalation settings page."""
import json
from types import SimpleNamespace

import pytest

import app.services.staff_notifications as staff
import app.services.xai_realtime as realtime
from app.services.staff_notifications import (
    callback_request_payload,
    callback_staff_summary,
    notification_plan,
    notify_staff,
)
from tests.test_confirmation_pause import pending_voice


def _spa(settings, name="Test Spa"):
    return SimpleNamespace(id="spa-1", name=name, notification_settings=settings)


def _dashboard_settings(**overrides):
    # Exactly the shape PUT /booking-escalations/settings saves.
    saved = {
        "sms_destinations": ["+15551110001"],
        "email_destinations": [],
        "notifications_enabled": True,
        "staff_user_ids": [],
        "categories": [],
    }
    saved.update(overrides)
    return {"booking_escalation": saved}


class Recorder:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    async def __call__(self, destination, body):
        if destination in self.fail_for:
            raise RuntimeError("provider down")
        self.sent.append((destination, body))
        return "SM1"


@pytest.mark.parametrize("event", ["callback_requested", "booking_failed", "provider_error"])
def test_dashboard_recipients_get_attention_alerts(event):
    plan = notification_plan(_spa(_dashboard_settings()), event, "needs attention")
    assert plan["status"] == "ready"
    assert plan["sms_destinations"] == ["+15551110001"]


@pytest.mark.parametrize("event", ["booking_created", "cancelled", "rescheduled"])
def test_dashboard_recipients_do_not_get_routine_booking_news(event):
    plan = notification_plan(_spa(_dashboard_settings()), event, "routine")
    assert plan["status"] == "skipped"
    assert plan["reason"] == "no_destination"


def test_dashboard_switch_off_stops_alerts():
    plan = notification_plan(_spa(_dashboard_settings(notifications_enabled=False)), "callback_requested", "x")
    assert plan["status"] == "skipped"
    assert plan["reason"] == "disabled"


def test_legacy_top_level_settings_keep_working_and_are_merged_without_duplicates():
    settings = {"sms_destinations": ["+15551110001", "+15551110002"], **_dashboard_settings()}
    plan = notification_plan(_spa(settings), "callback_requested", "x")
    assert plan["sms_destinations"] == ["+15551110001", "+15551110002"]
    routine = notification_plan(_spa(settings), "booking_created", "x")
    assert routine["sms_destinations"] == ["+15551110001", "+15551110002"]


def test_legacy_event_filter_does_not_silence_dashboard_recipients():
    settings = {"sms_destinations": ["+15551110009"], "events": ["booking_created"], **_dashboard_settings()}
    plan = notification_plan(_spa(settings), "callback_requested", "x")
    assert plan["sms_destinations"] == ["+15551110001"]


def test_malformed_settings_are_ignored_not_crashing():
    for settings in (None, [], {"booking_escalation": "yes"}, {"booking_escalation": {"sms_destinations": "+1555"}}):
        assert notification_plan(_spa(settings), "callback_requested", "x")["status"] == "skipped"


@pytest.mark.asyncio
async def test_one_failing_number_does_not_stop_the_others():
    spa = _spa(_dashboard_settings(sms_destinations=["+15551110001", "+15551110002"]))
    sender = Recorder(fail_for={"+15551110001"})
    result = await notify_staff(spa, "callback_requested", "call back", sender=sender)
    assert result["status"] == "sent"
    assert result["sms_sent"] == 1
    assert [phone for phone, _ in sender.sent] == ["+15551110002"]


@pytest.mark.asyncio
async def test_dashboard_email_recipients_use_the_mail_transport():
    spa = _spa(_dashboard_settings(sms_destinations=[], email_destinations=["desk@example.test"]))
    mails = []

    async def mailer(address, body):
        mails.append((address, body))
        return "accepted"

    result = await notify_staff(spa, "callback_requested", "call back", email_sender=mailer)
    assert result["status"] == "sent"
    assert result["emails_sent"] == 1
    assert mails == [("desk@example.test", "call back")]

    async def unconfigured(address, body):
        return "not_configured"

    result = await notify_staff(spa, "callback_requested", "call back", email_sender=unconfigured)
    assert result["status"] == "skipped"
    assert result["email"] == "not_configured"


def test_callback_summary_tells_staff_who_to_call_and_redacts_cards():
    payload = callback_request_payload(
        spa_id="spa-1",
        call_sid="CA1",
        caller_phone="+15550001234",
        caller_name="Test Alice",
        reason="facial question, card 4111111111111111",
        preferred_window="tomorrow afternoon",
    )
    text = callback_staff_summary(payload, "Test Spa")
    assert "Test Spa" in text
    assert "Test Alice" in text
    assert "+15550001234" in text
    assert "tomorrow afternoon" in text
    assert "Nothing is booked yet." in text
    assert "4111" not in text


def test_callback_summary_without_phone_says_so():
    payload = callback_request_payload(spa_id="s", call_sid="CA1", caller_phone=None,
                                       caller_name=None, reason=None, preferred_window=None)
    assert "Phone: not available" in callback_staff_summary(payload)


@pytest.mark.asyncio
async def test_saved_callback_texts_the_dashboard_recipient(monkeypatch):
    voice = pending_voice()
    voice.session.entities["callback_authorized"] = True
    voice.session.direction = "inbound"
    voice.session.from_number = "+15550001234"
    spa = _spa(_dashboard_settings(), name="Test Spa")
    added = []

    class FakeDb:
        def add(self, row):
            added.append(row)

        async def commit(self):
            return None

        async def flush(self):
            return None

    class FakeSessionLocal:
        async def __aenter__(self):
            return FakeDb()

        async def __aexit__(self, *exc):
            return False

    async def fake_prepare(db, session):
        return SimpleNamespace(spa=spa)

    sender = Recorder()
    real_notify = staff.notify_staff

    async def notify_with_recorder(spa_arg, event, summary, **kwargs):
        return await real_notify(spa_arg, event, summary, sender=sender)

    monkeypatch.setattr(realtime, "AsyncSessionLocal", FakeSessionLocal)
    monkeypatch.setattr(realtime, "_prepare", fake_prepare)
    monkeypatch.setattr(staff, "notify_staff", notify_with_recorder)

    result = json.loads(await voice._run_request_callback(json.dumps({
        "caller_name": "Test Alice",
        "reason": "question about a facial",
        "preferred_window": "after 3 pm",
    })))
    assert result["status"] == "stored"
    assert len(added) == 1
    assert len(sender.sent) == 1
    phone, body = sender.sent[0]
    assert phone == "+15551110001"
    assert "Test Alice" in body and "+15550001234" in body and "after 3 pm" in body
