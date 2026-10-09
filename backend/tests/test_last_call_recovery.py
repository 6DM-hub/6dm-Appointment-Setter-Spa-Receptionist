import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from tests.test_confirmation_pause import pending_voice
from app.services.booking_state import get_draft, save_draft
from app.services.appointment_booking_service import _square_verified

def test_other_verified_opening_remains_grounded_after_selection():
    voice = pending_voice()
    draft = get_draft(voice.session)
    draft.alternative_slots = []
    draft.verified_availability = {"slots": [{"start": "2026-10-09T17:15:00Z"}]}
    save_draft(voice.session, draft)
    assert voice._is_time_grounded("2026-10-09T12:15:00-05:00")
    assert not voice._is_time_grounded("2026-10-09T12:20:00-05:00")

def test_capacity_authority_requires_actual_square_slot():
    verdict = SimpleNamespace(available=True, slot={"start": "2026-10-09T17:15:00Z"})
    assert _square_verified(verdict, SimpleNamespace(provider="square"))
    assert not _square_verified(verdict, SimpleNamespace(provider="local"))
    verdict.slot = None
    assert not _square_verified(verdict, SimpleNamespace(provider="square"))

@pytest.mark.asyncio
async def test_already_spoken_ack_does_not_suppress_thinking_sound():
    voice = pending_voice()
    voice._ws = object()
    voice._hold_ack_played_this_turn = True
    voice.HOLD_ACK_DELAY_SECONDS = voice.HOLD_TONE_DELAY_SECONDS = 0
    voice.start_hold_tone = AsyncMock()
    await voice._maybe_hold_ack()
    voice.start_hold_tone.assert_awaited_once()
    assert voice.spoken == []

@pytest.mark.asyncio
async def test_callback_needs_permission_and_offer_does_not_repeat():
    voice = pending_voice()
    assert json.loads(await voice._run_request_callback("{}"))["status"] == "consent_required"
    await voice._offer_staff_callback()
    await voice._offer_staff_callback()
    assert len(voice.spoken) == 1
    voice._run_request_callback = AsyncMock(return_value='{"status":"stored"}')
    assert not await voice._confirm_pending_booking_from_caller("")
    voice._run_request_callback.assert_not_awaited()
    assert await voice._confirm_pending_booking_from_caller("yes please")
    voice._run_request_callback.assert_awaited_once()
    assert not voice.session.entities.get("callback_offer_pending")

@pytest.mark.asyncio
async def test_selecting_one_opening_keeps_others_and_square_overrides_local_aggregate(monkeypatch):
    from tests.test_square_availability_grounding import (_patch_booking, _FakeDB, _session, _slot, START, _SelectingCalendar)
    from app.services.booking_state import stage, remember_verified_availability
    from app.services.grok_service import AppointmentIntent
    from app.services.appointment_booking_service import stage_booking, BookingOutcome
    from datetime import timedelta
    calendar = _SelectingCalendar(revalidation_available=True)
    _patch_booking(monkeypatch, calendar)
    async def stale_local_conflict(*args, **kwargs):
        return True
    monkeypatch.setattr("app.services.appointment_booking_service._has_conflict", stale_local_conflict)
    session = _session()
    intent = AppointmentIntent(confidence=1, intent="schedule", service_description="Swedish massage", requested_start_iso=START.isoformat())
    stage(session, intent)
    other = START + timedelta(minutes=15)
    remember_verified_availability(session, [_slot(START), _slot(other)], service="Swedish massage", staff=None, location_id="LOC", duration_minutes=60, date_iso="2026-10-02", source="alternative_search")
    result = await stage_booking(_FakeDB(), session, intent)
    assert result.outcome == BookingOutcome.DRAFT
    draft = get_draft(session)
    assert len(draft.alternative_slots) == 2
    assert len(draft.verified_availability["slots"]) == 2

