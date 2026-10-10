import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
import pytest
from tests.test_availability_retry_repair import voice
from app.services.booking_state import get_draft


def say(v,text):
    v._pending_caller=text
    v._flush_caller_turn()


def tomorrow(v,hour=15):
    return (datetime.now(v._tz)+timedelta(days=1)).replace(hour=hour,minute=0,second=0,microsecond=0)


@pytest.mark.parametrize('time',['3 PM','three PM','3 P M'])
@pytest.mark.parametrize('detail',['With SIX please','My name is Test Alice'])
def test_explicit_time_survives_details_before_first_lookup(time,detail):
    v=voice()
    say(v,'Tomorrow at '+time)
    say(v,detail)
    assert v._is_time_grounded(tomorrow(v).isoformat())
    assert not v._is_time_grounded(tomorrow(v,16).isoformat())
    assert not get_draft(v.session).provider_verified
    assert not get_draft(v.session).confirmation_authorized


def test_date_and_time_in_separate_turns_survive_provider_answer():
    v=voice()
    say(v,'Tomorrow')
    say(v,'3 PM')
    say(v,'SIX please')
    assert v._is_time_grounded(tomorrow(v).isoformat())


@pytest.mark.parametrize('correction',['Actually a different day','Never mind that','Tomorrow at 4 PM'])
def test_correction_does_not_reuse_old_caller_time(correction):
    v=voice()
    say(v,'Tomorrow at 3 PM')
    say(v,correction)
    say(v,'With SIX')
    assert not v._is_time_grounded(tomorrow(v).isoformat())


def test_bare_three_does_not_invent_meridiem_or_availability():
    v=voice()
    say(v,'Tomorrow')
    say(v,'three')
    assert not v._is_time_grounded(tomorrow(v).isoformat())


async def test_repeated_ungrounded_slot_across_turns_offers_callback(monkeypatch):
    v=voice()
    v._send_function_output=AsyncMock()
    v._offer_staff_callback=AsyncMock()
    v._persist_session=AsyncMock()
    v._run_propose_appointment=AsyncMock()
    say(v,'Tomorrow')
    for i,utterance in enumerate(['three','yes that time']):
        say(v,utterance)
        await v._handle_function_call({'name':'propose_appointment','call_id':str(i),'arguments':json.dumps({'requested_start_iso':tomorrow(v).isoformat(),'service_description':'60 minute deep tissue massage','preferred_staff':'SIX'})})
    outputs=[json.loads(call.args[1]) for call in v._send_function_output.await_args_list]
    assert [o['status'] for o in outputs]==['ungrounded_time','needs_staff_help']
    assert outputs[0]['message']=='Which date and time would you like? Please include AM or PM.'
    v._offer_staff_callback.assert_awaited_once()
    v._run_propose_appointment.assert_not_awaited()
    say(v,'Tomorrow at 3 PM')
    say(v,'With SIX')
    assert v._is_time_grounded(tomorrow(v).isoformat())
