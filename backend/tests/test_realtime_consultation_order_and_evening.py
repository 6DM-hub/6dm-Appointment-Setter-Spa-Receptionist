"""Regressions for consultation order and caller-selected evening windows.

The realtime model can call the booking tools before it has finished Cara's
consultative questions.  The server owns this sequence: finish the required
questions and optional soft offers first, ask for the caller's preferred time
when it is still missing, and only then ask the provider for openings.
"""

import json
from datetime import datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from app.services import appointment_booking_service as booking
from app.services.booking_state import get_draft
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from app.services.xai_realtime import XAIVoiceSession
from tests.test_booking_intent_state import world


MASSAGE_SERVICES = [
    {
        "name": "30 Minute Swedish Massage",
        "duration_minutes": 30,
        "service_family": "Swedish Massage",
        "consultation_kind": "massage",
    },
    {
        "name": "60 Minute Swedish Massage",
        "duration_minutes": 60,
        "service_family": "Swedish Massage",
        "consultation_kind": "massage",
    },
]


def voice() -> XAIVoiceSession:
    session = CallSession(
        "consultation-order-call",
        "inbound",
        "+15550000002",
        "+15550000001",
        business_name="Test Spa",
        tenant_id="tenant-1",
        timezone="America/Chicago",
    )
    result = XAIVoiceSession(
        "consultation-order-call",
        session,
        now_provider=lambda tz: datetime(2026, 10, 10, 12, 0, tzinfo=tz),
    )
    result._persist_session = AsyncMock()
    result._send = AsyncMock()
    return result


def caller_says(result: XAIVoiceSession, text: str) -> None:
    result._pending_caller = text
    result._flush_caller_turn()


def proposal_event(call_id: str = "proposal") -> dict:
    return {
        "type": "response.function_call_arguments.done",
        "name": "propose_appointment",
        "call_id": call_id,
        "arguments": json.dumps(
            {
                "requested_services": ["60 Minute Swedish Massage"],
                "service_description": "60 Minute Swedish Massage",
                "preferred_staff": "SIX",
                # Deliberately omit a time. Model output cannot invent one.
            }
        ),
    }


@pytest.mark.asyncio
async def test_completed_consultation_day_without_time_asks_best_time_before_provider():
    result = voice()
    caller_says(result, "I need a 60-minute Swedish massage next Wednesday.")
    result._enforce_service_consultation = AsyncMock(return_value=False)
    result._send_function_output = AsyncMock()
    result._send_force_message = AsyncMock()
    result._cancel_active_response = AsyncMock()
    result._offer_spoken_window = AsyncMock()
    result._run_propose_appointment = AsyncMock()

    await result._handle_function_call(proposal_event())

    result._enforce_service_consultation.assert_awaited_once()
    result._send_force_message.assert_awaited_once_with(
        "What's the best time for you to come in?"
    )
    result._offer_spoken_window.assert_not_awaited()
    result._run_propose_appointment.assert_not_awaited()
    output = json.loads(result._send_function_output.await_args.args[1])
    assert output["status"] == "missing_day_part"
    assert output["message"] == "What's the best time for you to come in?"


@pytest.mark.asyncio
async def test_incomplete_consultation_blocks_time_question_and_provider_lookup():
    result = voice()
    caller_says(result, "I need a 60-minute Swedish massage next Wednesday.")
    result._enforce_service_consultation = AsyncMock(return_value=True)
    result._send_function_output = AsyncMock()
    result._send_force_message = AsyncMock()
    result._offer_spoken_window = AsyncMock()
    result._run_propose_appointment = AsyncMock()

    await result._handle_function_call(proposal_event("consult-first"))

    result._enforce_service_consultation.assert_awaited_once()
    result._send_force_message.assert_not_awaited()
    result._offer_spoken_window.assert_not_awaited()
    result._run_propose_appointment.assert_not_awaited()


@pytest.mark.asyncio
async def test_evening_survives_all_consultation_turns_and_reaches_provider_search():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(MASSAGE_SERVICES, []))
    result._speak_consultation_gate = AsyncMock()
    caller_says(
        result,
        "I need a 60-minute Swedish massage with SIX next Wednesday evening.",
    )
    args = {
        "requested_services": ["60 Minute Swedish Massage"],
        "service_description": "60 Minute Swedish Massage",
        "preferred_staff": "SIX",
    }

    assert await result._enforce_service_consultation("q-1", args) is True
    for index, answer in enumerate(
        (
            "Mostly relaxation.",
            "My shoulders.",
            "Medium pressure.",
            "No injuries or areas to avoid.",
            "Keep the 60-minute option.",
        ),
        start=2,
    ):
        caller_says(result, answer)
        consumed = await result._enforce_service_consultation(
            f"q-{index}", {"requested_services": ["Swedish Massage"]}
        )

    assert consumed is False
    start, end = result._spoken_day_part_window()
    assert start.date().isoformat() == "2026-10-14"
    assert (start.hour, end.hour) == (17, 21)

    result._offer_spoken_window = AsyncMock()
    result._send_function_output = AsyncMock()
    await result._handle_function_call(proposal_event("evening-search"))

    result._offer_spoken_window.assert_awaited_once()
    provider_window = result._offer_spoken_window.await_args.args[1]
    provider_args = result._offer_spoken_window.await_args.args[2]
    assert (provider_window[0].hour, provider_window[1].hour) == (17, 21)
    assert provider_args["preferred_staff"] == "SIX"
    assert provider_args["requested_services"] == ["60 Minute Swedish Massage"]


@pytest.mark.asyncio
async def test_evening_search_discards_provider_slots_outside_evening(world):
    chicago = ZoneInfo("America/Chicago")
    window_start = datetime(2026, 10, 14, 17, 0, tzinfo=chicago)
    window_end = datetime(2026, 10, 14, 21, 0, tzinfo=chicago)
    provider_slots = [
        datetime(2026, 10, 14, 16, 45, tzinfo=chicago),
        datetime(2026, 10, 14, 17, 15, tzinfo=chicago),
        datetime(2026, 10, 14, 19, 30, tzinfo=chicago),
        datetime(2026, 10, 14, 20, 45, tzinfo=chicago),
        datetime(2026, 10, 14, 21, 0, tzinfo=chicago),
    ]
    world["adapter"].list_openings = AsyncMock(
        return_value=[
            {
                "start": start.isoformat(),
                "duration_minutes": 60,
                "service_variation_id": "var_swedish",
                "service_variation_version": 1,
                "team_member_id": "TM_1",
                "location_id": "loc_1",
            }
            for start in provider_slots
        ]
    )
    intent = AppointmentIntent(
        confidence=1.0,
        intent="schedule",
        service_description="60 Minute Swedish Massage",
        requested_services=["60 Minute Swedish Massage"],
        preferred_staff="SIX",
    )

    result = await booking.search_day_part(
        world["db"], world["session"], intent, window_start, window_end
    )

    offered = [
        datetime.fromisoformat(slot["start"]).astimezone(chicago)
        for slot in get_draft(world["session"]).alternative_slots
    ]
    assert [(item.hour, item.minute) for item in offered] == [
        (17, 15),
        (19, 30),
        (20, 45),
    ]
    assert "04:45 PM" not in result.message
    assert "09:00 PM" not in result.message

