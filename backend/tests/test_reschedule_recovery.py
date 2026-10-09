import json
from unittest.mock import AsyncMock
import pytest
from tests.test_confirmation_pause import pending_voice
from app.services.booking_state import get_draft

def event(call_id, args, name="propose_appointment"):
    return {"type":"response.function_call_arguments.done", "name":name, "call_id":call_id,
            "arguments":json.dumps(args)}

@pytest.mark.asyncio
async def test_missing_reschedule_time_prompts_once_without_provider_retry():
    voice = pending_voice()
    voice._run_propose_appointment = AsyncMock()
    args = {"operation":"reschedule", "appointment_id":"existing", "guest_name":"None"}
    await voice._handle_function_call(event("first",args))
    await voice._handle_function_call(event("second",args))
    voice._run_propose_appointment.assert_not_awaited()
    questions=[p for p in voice.spoken if p.get("item",{}).get("type")=="force_message"]
    assert len(questions)==1
    assert "What day and time" in str(questions)
    assert get_draft(voice.session).operation_mode == "reschedule"
    assert not any(p.get("type")=="response.create" for p in voice.spoken)
    voice._user_turn_count += 1
    await voice._handle_function_call(event("third",args))
    assert voice.session.entities.get("callback_offer_pending")
    questions=[p for p in voice.spoken if p.get("item",{}).get("type")=="force_message"]
    assert len(questions)==2
    assert "call you back" in str(questions[-1])
    await voice._handle_function_call(event("fourth",args))
    assert len([p for p in voice.spoken if p.get("item",{}).get("type")=="force_message"])==2

@pytest.mark.asyncio
async def test_chain_limit_offers_callback_instead_of_silence():
    voice = pending_voice()
    voice._tool_chain_depth_this_turn = voice.MAX_TOOL_CHAIN_DEPTH_PER_TURN
    await voice._handle_function_call(event("limit",{},"lookup_spa_facts"))
    assert voice.session.entities.get("callback_offer_pending")
    assert "call you back" in str(voice.spoken)

@pytest.mark.asyncio
async def test_callback_pending_cannot_confirm_or_restart_booking():
    voice = pending_voice()
    voice.session.entities["callback_offer_pending"] = True
    voice._run_confirm_appointment = AsyncMock()
    await voice._handle_function_call(event("blocked",{},"confirm_appointment"))
    voice._run_confirm_appointment.assert_not_awaited()
    assert "awaiting_callback_consent" in str(voice.spoken)

def test_negative_availability_clause_does_not_interrupt_valid_alternatives():
    from app.services.booking_state import contains_unauthorized_availability_claim
    from zoneinfo import ZoneInfo
    voice = pending_voice()
    assert not contains_unauthorized_availability_claim("2 PM isn't available, but I have",voice.session,ZoneInfo("America/Chicago"))
    assert contains_unauthorized_availability_claim("2 PM isn't available, but 3 PM is available",voice.session,ZoneInfo("America/Chicago"))

@pytest.mark.asyncio
async def test_clarification_clears_deferred_model_retries():
    voice = pending_voice()
    voice._response_needed_after_tool = True
    voice._restricted_response_needed_after_tool = True
    await voice._handle_function_call(event("missing",{"operation":"reschedule"}))
    assert not voice._response_needed_after_tool
    assert not voice._restricted_response_needed_after_tool
