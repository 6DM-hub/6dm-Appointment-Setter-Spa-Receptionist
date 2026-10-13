"""Caller requests for openings must bypass the missing-time question."""
import json
from unittest.mock import AsyncMock
import pytest
from tests.test_realtime_consultation_order_and_evening import voice, caller_says, proposal_event

@pytest.mark.asyncio
@pytest.mark.parametrize("question", ["What times do you have available?", "What time do you have available?", "Do you have any openings?"])
@pytest.mark.parametrize("earliest", [False, True])
async def test_known_day_openings_request_searches_instead_of_asking_again(question, earliest):
    v = voice()
    v._enforce_service_consultation = AsyncMock(return_value=False)
    v._offer_spoken_window = AsyncMock()
    v._send_function_output = AsyncMock()
    v._send_force_message = AsyncMock()
    v._offer_staff_callback = AsyncMock()
    caller_says(v, "Tuesday at 4:14 PM")
    caller_says(v, "Let's go Wednesday")
    caller_says(v, question)
    v.session.entities["time_preference_question_attempts"] = 2
    event = proposal_event()
    args = json.loads(event["arguments"])
    args["earliest"] = earliest
    args["requested_start_iso"] = "2026-10-13T16:14:00"
    event["arguments"] = json.dumps(args)
    await v._handle_function_call(event)
    v._offer_spoken_window.assert_awaited_once()
    start, end = v._offer_spoken_window.await_args.args[1]
    assert start.isoformat() == "2026-10-14T00:00:00-05:00"
    assert end.isoformat() == "2026-10-15T00:00:00-05:00"
    v._send_force_message.assert_not_awaited()
    v._offer_staff_callback.assert_not_awaited()

@pytest.mark.asyncio
async def test_unknown_date_openings_request_does_not_guess_a_window():
    v = voice()
    caller_says(v, "What times do you have available?")
    assert v._spoken_day_part_window() is None
