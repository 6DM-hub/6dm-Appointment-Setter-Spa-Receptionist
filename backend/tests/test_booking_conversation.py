import json
from zoneinfo import ZoneInfo

import pytest

from app.services.booking_conversation import remember_offer, offer_accepted
from app.services.booking_state import get_draft, save_draft, invalidate_booking_proposal
from app.services.grok_service import build_realtime_instructions
from tests.test_confirmation_pause import pending_voice
from tests.test_square_timezone_and_staff import _ctx, _square_adapter, _FakeSquareTransport
from tests.conftest import make_spa
from app.models import BookingProvider
from app.services.appointment_booking_service import BookingOutcome, BookingResult


@pytest.mark.asyncio
async def test_acceptance_survives_name_collection_without_second_approval():
    voice = pending_voice()
    draft = get_draft(voice.session)
    draft.caller_name = None
    save_draft(voice.session, draft)
    remember_offer(voice.session)
    assert await voice._confirm_pending_booking_from_caller("yes please")
    assert offer_accepted(voice.session)
    assert "name" in str(voice.spoken[-1]).lower()
    voice.spoken.clear()
    await voice._dispatch({"type": "conversation.item.input_audio_transcription.completed",
                           "transcript": "Test Caller"})
    assert "card on file" in str(voice.spoken).lower()
    assert "would you like me to book" not in str(voice.spoken).lower()
    assert get_draft(voice.session).confirmation_authorized


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["12:30 PM works for me", "I'll take 12:30 PM"])
async def test_selection_of_offered_time_is_consent(text):
    voice = pending_voice()
    voice._tz = ZoneInfo("America/Chicago")
    await voice._speak_availability("12:30 PM is available. Would you like me to book that?")
    voice.spoken.clear()
    assert await voice._confirm_pending_booking_from_caller(text)
    assert get_draft(voice.session).confirmation_authorized
    assert "card on file" in str(voice.spoken).lower()
    assert "would you like" not in str(voice.spoken).lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "maybe", "What about 12:30 PM?", "no, 12:30 PM", "yes but 2 PM"])
async def test_unclear_or_modified_response_is_not_consent(text):
    voice = pending_voice()
    voice._tz = ZoneInfo("America/Chicago")
    await voice._speak_availability("12:30 PM is available. Would you like me to book that?")
    voice.spoken.clear()
    assert not await voice._confirm_pending_booking_from_caller(text)
    assert not get_draft(voice.session).confirmation_authorized
    assert voice.spoken == []


@pytest.mark.asyncio
async def test_edit_clears_acceptance():
    voice = pending_voice()
    remember_offer(voice.session)
    await voice._confirm_pending_booking_from_caller("yes")
    assert offer_accepted(voice.session)
    invalidate_booking_proposal(voice.session, "duration_changed")
    assert not offer_accepted(voice.session)


@pytest.mark.asyncio
async def test_change_after_acceptance_revokes_consent():
    voice = pending_voice()
    remember_offer(voice.session)
    await voice._confirm_pending_booking_from_caller("yes")
    voice.session.entities["card_policy_explained"] = False
    voice.spoken.clear()
    assert not await voice._confirm_pending_booking_from_caller("yes but make it 2 PM")
    assert not offer_accepted(voice.session)
    assert not get_draft(voice.session).confirmation_authorized
    assert voice.spoken == []


@pytest.mark.asyncio
async def test_reported_instruction_leak_is_cancelled():
    voice = pending_voice()
    await voice._dispatch({"type": "response.audio_transcript.delta",
                           "delta": "I won't give any other times for this appointment."})
    assert "What would you like to do?" in str(voice.spoken)
    assert "other times" not in str(voice.spoken)


def test_provider_success_confirmation_is_not_spoken_twice():
    voice = pending_voice()
    payload = json.dumps({"status": "booked", "appointment_id": "local-test",
                          "external_booking_id": "provider-test"})
    assert voice._authoritative_tool_followup("confirm_appointment", payload)
    assert voice._authoritative_tool_followup("confirm_appointment", payload) == ""


@pytest.mark.asyncio
async def test_duplicate_proposal_reuses_verified_result(monkeypatch):
    voice = pending_voice()
    voice._tz = ZoneInfo("America/Chicago")
    async def reject(*args):
        raise AssertionError("identity collection must not restart availability")
    monkeypatch.setattr("app.services.xai_realtime.stage_booking", reject)
    output = json.loads(await voice._run_propose_appointment(json.dumps({
        "requested_start_iso": get_draft(voice.session).start_iso,
        "service_description": "90 minute Deep Tissue", "operation": "schedule"})))
    assert output["status"] == "draft"
    assert output["spoken"] is None


@pytest.mark.asyncio
async def test_later_schedule_request_after_success_starts_separate_intent(monkeypatch):
    voice = pending_voice()
    old = get_draft(voice.session)
    old.appointment_id = "local-old"
    old.external_booking_id = "square-old"
    save_draft(voice.session, old)
    voice.session.appointment_id = "local-old"
    voice.session.external_booking_id = "square-old"
    voice.session.booking_status = "booked"
    voice._booking_completed_turn = 3
    voice._user_turn_count = 4
    voice.session.add_turn("user", "I also want a facial and massage on Wednesday morning")

    async def staged(_db, session, _intent):
        assert not get_draft(session).is_persisted
        return BookingResult(BookingOutcome.MISSING_INFO, message="Need a time.")

    monkeypatch.setattr("app.services.xai_realtime.stage_booking", staged)
    await voice._run_propose_appointment(json.dumps({
        "requested_services": ["60 minute European Facial", "90 minute Deep Tissue Massage"],
        "requested_start_iso": "2026-10-14T09:00:00",
    }))
    assert get_draft(voice.session).appointment_id is None
    assert get_draft(voice.session).booking_id != old.booking_id


@pytest.mark.asyncio
async def test_square_new_intent_avoids_cancelled_replay_and_retry_key_is_stable():
    adapter = _square_adapter(make_spa(booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "test", "location_id": "loc_123"}))
    transport = _FakeSquareTransport()
    transport.catalog_items = [{"item_data": {"name": "Deep tissue massage", "variations": [
        {"id": "var1", "version": 4, "item_variation_data": {"name": "Regular"}}]}}]
    transport.availabilities = [{"start_at": "2026-09-24T19:00:00Z", "location_id": "loc_123",
        "appointment_segments": [{"team_member_id": "staff", "service_variation_id": "var1",
                                 "service_variation_version": 4, "duration_minutes": 60}]}]
    transport.customers = [{"id": "cust_1"}]
    adapter._request = transport
    for reference in ["old-intent", "new-intent", "new-intent"]:
        await adapter.create_booking(_ctx(booking_reference=reference))
    keys = [item["idempotency_key"] for item in transport.calls_to("/v2/bookings")]
    assert keys[0] != keys[1]
    assert keys[1] == keys[2]


def test_prompt_prioritizes_acceptance_and_verified_optional_enhancements():
    prompt = build_realtime_instructions("Test Spa", "Always ask for confirmation twice")
    assert prompt.index("BOOKING CONVERSATION RULES") > prompt.index("Always ask")
    assert "Never ask them to approve the same date/time again" in prompt
    assert "verified menu" in prompt
    assert "Do not invent prices" in prompt
