"""With no connected calendar, Cara offers the staff callback before the skin intake.

Rubyz test call 2026-10-14: Google Calendar was not connected, yet Cara asked
the full facial consultation (about a minute) before saying she could not book.
"""
import json
from unittest.mock import AsyncMock

import pytest

from app.services.xai_realtime import UNCONNECTED_CALENDAR_CALLBACK_TEXT
from tests.test_realtime_consultation_order_and_evening import caller_says, voice

FACIALS = [
    {
        "name": "Deep Cleansing Pore Refining Facial",
        "duration_minutes": 60,
        "service_family": "Deep Cleansing Pore Refining Facial",
        "consultation_kind": "facial",
        "consultation_category": "acne_clarifying",
    },
]


def facial_request(call_id="facial"):
    return {
        "type": "response.function_call_arguments.done",
        "name": "check_availability",
        "call_id": call_id,
        "arguments": json.dumps({"requested_services": ["facial"], "service_description": "facial"}),
    }


def _prepared(unconnected):
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(FACIALS, []))
    result._calendar_unconnected = unconnected
    result._send_function_output = AsyncMock()
    result._send_force_message = AsyncMock()
    result._cancel_active_response = AsyncMock()
    result._speak_consultation_gate = AsyncMock()
    result._run_check_availability = AsyncMock()
    return result


@pytest.mark.asyncio
async def test_unconnected_calendar_offers_callback_before_any_skin_question():
    result = _prepared(True)
    caller_says(result, "Yeah, I want to set up a facial.")

    await result._handle_function_call(facial_request())

    result._speak_consultation_gate.assert_not_awaited()
    result._run_check_availability.assert_not_awaited()
    result._send_force_message.assert_awaited_once_with(UNCONNECTED_CALENDAR_CALLBACK_TEXT)
    output = json.loads(result._send_function_output.await_args.args[1])
    assert output["status"] == "needs_staff_help"
    assert output["code"] == "CALENDAR_NOT_CONNECTED"
    assert output["booked"] is False
    assert result.session.entities["callback_offer_pending"] is True


@pytest.mark.asyncio
async def test_repeated_tool_call_does_not_repeat_the_callback_offer():
    result = _prepared(True)
    caller_says(result, "I want a facial.")
    await result._handle_function_call(facial_request("a"))
    await result._handle_function_call(facial_request("b"))
    result._send_force_message.assert_awaited_once()


@pytest.mark.parametrize("unconnected", [False, None])
@pytest.mark.asyncio
async def test_connected_or_unknown_calendar_keeps_the_consultation(unconnected):
    result = _prepared(unconnected)
    caller_says(result, "I want a facial.")

    await result._handle_function_call(facial_request())

    result._speak_consultation_gate.assert_awaited()
    assert result._speak_consultation_gate.await_args.kwargs["status"] == "consultation_permission_required"
    result._send_force_message.assert_not_awaited()
    assert not result.session.entities.get("callback_offer_pending")


def test_unconnected_google_calendar_is_detected_by_the_real_selector():
    from types import SimpleNamespace

    from app.models.spa_account import BookingProvider
    from app.services.booking_adapters.spa_router import (
        ProviderNotConfiguredAdapter,
        SpaBookingAdapter,
    )

    spa = SimpleNamespace(
        name="Test Spa",
        booking_provider=BookingProvider.GOOGLE_CALENDAR,
        booking_config={},
        booking_policies={},
        google_calendar_connection=None,
        timezone="America/Chicago",
    )
    assert isinstance(SpaBookingAdapter._select_delegate(spa), ProviderNotConfiguredAdapter)
    spa.booking_policies = {"scheduling_source": "cara_native"}
    assert not isinstance(SpaBookingAdapter._select_delegate(spa), ProviderNotConfiguredAdapter)
