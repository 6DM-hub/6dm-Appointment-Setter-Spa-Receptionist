import asyncio
import json
from datetime import timedelta

import pytest

from app.services.booking_state import (
    BookingDraft, get_draft, mark_read_back, proposal_fingerprint, save_draft,
)
from app.services.call_state import CallSession
from app.models import BookingProvider
from app.services.media_bridge import TwilioMediaBridge
from app.services.xai_realtime import XAIVoiceSession
from tests.test_square_timezone_and_staff import _ctx, _square_adapter, _FakeSquareTransport
from tests.conftest import make_spa


def pending_voice(cls=XAIVoiceSession, socket=None):
    session = CallSession(call_sid="CApause", direction="inbound", business_name="Test Spa",
                          from_number="+15550001", to_number="+15550002")
    session.booking_status = "awaiting_confirmation"
    session.entities["card_on_file_required"] = True
    draft = BookingDraft(caller_name="Test Caller", service_description="Deep Tissue",
                         start_iso="2026-10-10T12:30:00", provider_verified=True,
                         selected_slot={"start": "2026-10-10T17:30:00Z", "duration_minutes": 90})
    draft.selected_slot_revision = draft.draft_revision
    draft.verified_fingerprint = proposal_fingerprint(draft)
    save_draft(session, draft)
    mark_read_back(session)
    voice = cls("CApause", session, socket) if socket else cls("CApause", session)
    voice.spoken = []

    async def send(payload):
        voice.spoken.append(payload)

    async def noop(*args):
        pass

    voice._send = send
    voice._cancel_active_response = noop
    voice._persist_session = noop
    return voice


@pytest.mark.asyncio
async def test_approval_question_has_no_automatic_card_followup_and_yes_gates_policy():
    voice = pending_voice()
    await voice._speak_availability("Would you like me to book that for you?")
    assert len(voice.spoken) == 1
    assert voice._after_availability_line is None
    assert not await voice._confirm_pending_booking_from_caller("")
    assert not await voice._confirm_pending_booking_from_caller("maybe")
    await voice._explain_card_policy()
    assert len(voice.spoken) == 1
    assert await voice._confirm_pending_booking_from_caller("yes")
    assert "card on file" in str(voice.spoken[-1])
    assert get_draft(voice.session).confirmation_authorized
    assert not get_draft(voice.session).is_persisted


@pytest.mark.asyncio
async def test_three_seconds_of_silence_only_prompts_never_consents():
    voice = pending_voice()
    voice._confirmation_played(get_draft(voice.session).draft_revision)
    await asyncio.sleep(2.9)
    assert voice.spoken == []
    await asyncio.sleep(0.2)
    assert "Would you like me to go ahead?" in str(voice.spoken)
    assert not get_draft(voice.session).confirmation_authorized
    assert "card" not in str(voice.spoken).lower()
    voice._cancel_confirmation_wait()


@pytest.mark.asyncio
async def test_caller_speech_cancels_followup_even_during_long_response():
    voice = pending_voice()
    voice._confirmation_played(get_draft(voice.session).draft_revision)
    await voice._dispatch({"type": "input_audio_buffer.speech_started"})
    await asyncio.sleep(3.1)
    assert voice.spoken == []
    assert not get_draft(voice.session).confirmation_authorized
    voice._cancel_confirmation_wait()


@pytest.mark.asyncio
async def test_twilio_mark_is_after_audio_and_timer_waits_for_ack():
    class Socket:
        def __init__(self):
            self.sent = []
            self.inbound = []
        async def send_text(self, text):
            self.sent.append(json.loads(text))
        async def receive_text(self):
            return json.dumps(self.inbound.pop(0) if self.inbound else {"event": "stop"})
    socket = Socket()
    voice = pending_voice(TwilioMediaBridge, socket)
    voice._stream_sid = "MZtest"
    voice._enqueue_audio("AA==")
    await voice._dispatch({"type": "response.done"})
    assert [p["event"] for p in socket.sent] == ["media", "mark"]
    assert getattr(voice, "_confirmation_wait_task", None) is None
    socket.inbound = [{"event": "mark", "mark": socket.sent[-1]["mark"]}]
    await voice._pump_twilio_to_xai()
    assert voice._confirmation_wait_task is not None
    voice._cancel_confirmation_wait()


@pytest.mark.asyncio
async def test_single_deep_tissue_matches_category_prefix_without_couples():
    adapter = _square_adapter(make_spa(booking_provider=BookingProvider.SQUARE,
                                     booking_config={"access_token": "test", "location_id": "loc_test"}))
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": name, "variations": [{"id": key,
            "item_variation_data": {"name": "Regular", "service_duration": minutes * 60000}}]}}
        for name, key, minutes in [
            ("Massage - Couples Deep Tissue Massage 90m", "couples", 90),
            ("Massage - Deep Tissue 60 Minutes", "single60", 60),
            ("Massage - Deep Tissue 90 Minutes", "single90", 90),
        ]
    ]
    adapter._request = transport
    ctx = _ctx(service_description="Deep Tissue", title="Deep Tissue")
    ninety = await adapter._resolve_service_variation(
        _ctx(service_description="Deep Tissue", title="Deep Tissue", end=ctx.start+timedelta(minutes=90)))
    assert ninety["id"] == "single90"
    sixty = await adapter._resolve_service_variation(ctx)
    assert sixty["id"] == "single60", "duration must be part of the resolution cache key"


@pytest.mark.asyncio
async def test_booking_waits_for_yes_and_card_playback_before_create():
    class Socket:
        def __init__(self):
            self.sent = []
            self.inbound = []
        async def send_text(self, text):
            self.sent.append(json.loads(text))
        async def receive_text(self):
            return json.dumps(self.inbound.pop(0) if self.inbound else {"event": "stop"})
    socket = Socket()
    voice = pending_voice(TwilioMediaBridge, socket)
    voice._stream_sid = "MZtest"
    assert json.loads(await voice._run_confirm_appointment())['booked'] is False
    voice.session.add_turn("user", "yes")
    voice._user_turn_count = 1
    writes = []
    async def create(raw):
        writes.append(raw)
        return json.dumps({"status": "booked", "appointment_id": "local-test",
                           "external_booking_id": "provider-test"})
    voice._run_confirm_appointment = create
    assert await voice._confirm_pending_booking_from_caller("yes")
    assert writes == []
    await voice._dispatch({"type": "response.done", "response": {"id": "old-model"}})
    assert socket.sent == [], "old model completion is not card playback completion"
    await voice._dispatch({"type": "response.created", "response": {"id": "card-speech"}})
    await voice._dispatch({"type": "response.done", "response": {"id": "card-speech"}})
    assert writes == []
    assert socket.sent[-1]["mark"]["name"].startswith("card-policy-")
    socket.inbound = [{"event": "mark", "mark": socket.sent[-1]["mark"]}]
    await voice._pump_twilio_to_xai()
    assert writes == []
    assert voice.session.entities["card_link_consent_pending_revision"] == 0
    assert await voice._confirm_pending_booking_from_caller("yes")
    assert len(writes) == 1


@pytest.mark.asyncio
async def test_modification_does_not_authorize_card_or_booking():
    voice = pending_voice()
    assert not await voice._confirm_pending_booking_from_caller("yes, but make it 2 PM")
    assert not get_draft(voice.session).confirmation_authorized
    assert voice.spoken == []
