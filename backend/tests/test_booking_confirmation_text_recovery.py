import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models import AppointmentStatus, CardStatus
from app.services.booking_conversation import remember_offer
from app.services.booking_state import get_draft
from tests.test_confirmation_pause import pending_voice


CONFIRMATION_TEXT_QUESTION = (
    "It looks like I'm having trouble on my end. "
    "The confirmation text can take up to ten seconds to arrive. "
    "I'll wait while you check. "
    "Did you receive a confirmation text for your appointment?"
)


def _provider_confirmed_appointment():
    """Appointment shaped like the read-only upcoming-booking lookup result."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        external_booking_id="square-confirmed-booking",
        status=AppointmentStatus.CONFIRMED,
        title="Deep Tissue",
        start_time=datetime(2026, 10, 10, 17, 30, tzinfo=timezone.utc),
        end_time=datetime(2026, 10, 10, 19, 0, tzinfo=timezone.utc),
        card_status=CardStatus.CARD_CONFIRMED,
    )


def _prepare_for_provider_error(voice):
    """Arm the existing accepted-offer path without requiring card playback."""
    voice.session.entities["card_on_file_required"] = False
    remember_offer(voice.session)
    async def uncertain_result(_raw_arguments):
        voice.session.entities["booking_reconciliation_required"] = True
        voice.session.booking_status = "follow_up_required"
        return json.dumps(
            {
                "status": "error",
                "booked": False,
                "appointment_id": None,
                "external_booking_id": None,
                "message": "The booking provider response could not be verified.",
            }
        )

    voice._run_confirm_appointment = AsyncMock(side_effect=uncertain_result)


@pytest.mark.asyncio
async def test_uncertain_provider_result_asks_exact_confirmation_text_question():
    voice = pending_voice()
    _prepare_for_provider_error(voice)

    assert await voice._confirm_pending_booking_from_caller("yes")

    assert voice._run_confirm_appointment.await_count == 1
    assert CONFIRMATION_TEXT_QUESTION in str(voice.spoken[-1])
    assert voice.session.entities["confirmation_text_check_pending"]["booking_id"]
    assert voice.session.booking_status not in {"booked", "rescheduled"}
    assert voice.session.appointment_id is None
    assert voice.session.external_booking_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "handler_name"),
    [
        ("confirm_appointment", "_run_confirm_appointment"),
        ("cancel_appointment", "_run_cancel_appointment"),
        ("manage_appointment", "_run_manage_appointment"),
    ],
)
async def test_confirmation_text_wait_blocks_booking_mutations_until_caller_answers(
    tool_name, handler_name
):
    voice = pending_voice()
    voice._arm_confirmation_text_check()
    handler = AsyncMock(
        side_effect=AssertionError(
            "booking mutations must wait for the caller's confirmation-text answer"
        )
    )
    setattr(voice, handler_name, handler)

    await voice._handle_function_call(
        {
            "type": "response.function_call_arguments.done",
            "name": tool_name,
            "call_id": f"blocked-{tool_name}",
            "arguments": "{}",
        }
    )

    handler.assert_not_awaited()
    outputs = [
        payload
        for payload in voice.spoken
        if payload.get("item", {}).get("type") == "function_call_output"
    ]
    assert outputs
    result = json.loads(outputs[-1]["item"]["output"])
    assert result["status"] == "awaiting_confirmation_text_answer"
    assert "Do not retry, replace, confirm, or cancel" in result["message"]
    assert voice.session.entities["confirmation_text_check_pending"]
    assert voice.session.booking_status not in {"booked", "rescheduled"}


@pytest.mark.asyncio
async def test_confirmation_text_yes_reconciles_read_only_without_creating_again():
    voice = pending_voice()
    voice._arm_confirmation_text_check()
    appointment = _provider_confirmed_appointment()
    voice._find_confirmation_text_booking = AsyncMock(return_value=appointment)
    voice._run_confirm_appointment = AsyncMock(
        side_effect=AssertionError("confirmation-text recovery must never create again")
    )

    assert await voice._handle_confirmation_text_answer(
        "Yes, I received the confirmation text."
    )

    voice._find_confirmation_text_booking.assert_awaited_once_with()
    voice._run_confirm_appointment.assert_not_awaited()
    assert voice.session.booking_status == "booked"
    assert voice.session.appointment_id == str(appointment.id)
    assert voice.session.external_booking_id == appointment.external_booking_id
    assert get_draft(voice.session).external_booking_id == appointment.external_booking_id
    assert "confirmed" in str(voice.spoken[-1]).casefold()
    assert "confirmation_text_check_pending" not in voice.session.entities


@pytest.mark.asyncio
async def test_confirmation_text_no_or_no_match_never_confirms_or_retries():
    no_text = pending_voice()
    no_text._arm_confirmation_text_check()
    no_text._find_confirmation_text_booking = AsyncMock()
    no_text._run_confirm_appointment = AsyncMock(
        side_effect=AssertionError("a no answer must not retry booking")
    )

    assert await no_text._handle_confirmation_text_answer("No, I did not get one.")
    no_text._find_confirmation_text_booking.assert_not_awaited()
    no_text._run_confirm_appointment.assert_not_awaited()
    assert no_text.session.booking_status not in {"booked", "rescheduled"}
    assert "you're all set" not in str(no_text.spoken[-1]).casefold()
    assert "call you back" in str(no_text.spoken[-1]).casefold()

    no_match = pending_voice()
    no_match._arm_confirmation_text_check()
    no_match._find_confirmation_text_booking = AsyncMock(return_value=None)
    no_match._run_confirm_appointment = AsyncMock(
        side_effect=AssertionError("an unverified text must not retry booking")
    )

    assert await no_match._handle_confirmation_text_answer(
        "Yes, I received a confirmation text."
    )
    no_match._find_confirmation_text_booking.assert_awaited_once_with()
    no_match._run_confirm_appointment.assert_not_awaited()
    assert no_match.session.booking_status not in {"booked", "rescheduled"}
    assert no_match.session.appointment_id is None
    assert no_match.session.external_booking_id is None
    assert "can't verify" in str(no_match.spoken[-1]).casefold()
    assert "confirmation_text_check_pending" not in no_match.session.entities


@pytest.mark.asyncio
async def test_confirmation_text_report_is_handled_before_normal_confirmation_loop():
    voice = pending_voice()
    voice._arm_confirmation_text_check()
    voice._find_confirmation_text_booking = AsyncMock(return_value=None)
    voice._run_confirm_appointment = AsyncMock(
        side_effect=AssertionError(
            "reported provider text must not enter another booking write"
        )
    )

    await voice._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "Yes, it's already confirmed. I just got the text message.",
        }
    )

    voice._find_confirmation_text_booking.assert_awaited_once_with()
    voice._run_confirm_appointment.assert_not_awaited()
    assert voice.session.booking_status not in {"booked", "rescheduled"}
    assert "can't verify" in str(voice.spoken[-1]).casefold()
