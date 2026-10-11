"""Regressions from Cara's European Facial -> acne recommendation call.

No live phone, Square, Twilio, Redis, or calendar connection is used.
"""
from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.services.consultation_gate import (
    initialize_consultation,
    record_consultation_answer,
    record_consultation_permission,
    take_duration_offer,
)
from tests.test_realtime_consultation_enforcement import caller_says, voice


SERVICES = [
    {
        "name": "Petite Facial",
        "duration_minutes": 30,
        "consultation_kind": "facial",
        "consultation_category": "custom",
    },
    {
        "name": "European Facial",
        "duration_minutes": 60,
        "consultation_kind": "facial",
        "consultation_category": "custom",
    },
    {
        "name": "Acne Clarifying Facial",
        "duration_minutes": 60,
        "consultation_kind": "facial",
        "consultation_category": "acne_clarifying",
    },
]


def _original_args():
    return {
        "requested_services": ["60-minute European Facial"],
        "service_description": "60-minute European Facial",
    }


async def _reach_recommendation(result):
    result._consultation_catalog = AsyncMock(return_value=(SERVICES, []))
    caller_says(result, "I want a 60-minute European Facial.")
    assert await result._enforce_service_consultation("permission", _original_args())
    assert "few quick questions" in result._speak_consultation_gate.await_args.kwargs["spoken"]

    caller_says(result, "Sure.")
    assert await result._enforce_service_consultation("main", _original_args())
    assert "main thing bothering" in result._speak_consultation_gate.await_args.kwargs["spoken"]

    caller_says(result, "My skin is oily and I have acne.")
    assert await result._enforce_service_consultation("sensitive", _original_args())
    assert "sensitivity" in result._speak_consultation_gate.await_args.kwargs["spoken"]
    assert "active breakouts" not in result._speak_consultation_gate.await_args.kwargs["spoken"]

    caller_says(result, "No.")
    assert await result._enforce_service_consultation("history", _original_args())
    assert "had facials before" in result._speak_consultation_gate.await_args.kwargs["spoken"]

    caller_says(result, "I've had facials before. I love massaging the face, lymphatic drainage, and gua sha.")
    assert await result._enforce_service_consultation("recommendation", _original_args())
    said = result._speak_consultation_gate.await_args.kwargs["spoken"]
    assert "Acne Clarifying Facial" in said
    assert "European Facial" in said
    assert "30-minute" not in said
    assert "would you like to switch" in said.lower()


@pytest.mark.asyncio
async def test_named_european_facial_acne_recommendation_requires_caller_acceptance():
    result = voice()
    await _reach_recommendation(result)

    caller_says(result, "Yes, that sounds good.")
    args = _original_args()
    assert await result._enforce_service_consultation("accepted", args) is False
    assert args["requested_services"] == ["Acne Clarifying Facial"]
    state = result.session.entities["consultation_states"]["facial"]
    assert state["recommendation_status"] == "accepted"
    assert state["selected_duration_minutes"] == 60
    assert state["selected_service_name"] == "Acne Clarifying Facial"
    assert "facial" in result.session.entities["consultation_completed_kinds"]

    # A stale model argument on the next turn cannot silently restore European.
    caller_says(result, "2:30 PM.")
    args = _original_args()
    assert await result._enforce_service_consultation("time", args) is False
    assert args["requested_services"] == ["Acne Clarifying Facial"]
    assert result.session.entities["caller_grounded_requested_time"] == "14:30:00"


@pytest.mark.asyncio
async def test_declining_alternative_keeps_original_service_and_duration():
    result = voice()
    await _reach_recommendation(result)
    caller_says(result, "No thanks, keep the European Facial.")
    args = _original_args()
    assert await result._enforce_service_consultation("declined", args) is False
    assert args["requested_services"] == ["European Facial"]
    state = result.session.entities["consultation_states"]["facial"]
    assert state["recommendation_status"] == "declined"
    assert state["selected_duration_minutes"] == 60


@pytest.mark.asyncio
async def test_recommendation_waits_for_caller_and_never_autoswitches():
    result = voice()
    await _reach_recommendation(result)
    said_count = result._speak_consultation_gate.await_count
    # Duplicate tool call, same caller turn, no new consent.
    assert await result._enforce_service_consultation("duplicate", _original_args())
    assert result._speak_consultation_gate.await_count == said_count
    assert "facial" not in result.session.entities.get("consultation_completed_kinds", [])


def test_duration_offer_keeps_explicit_menu_service_without_downselling():
    state = initialize_consultation("60-minute European Facial", services=SERVICES)
    assert state is not None
    state = record_consultation_permission(state, accepted=False)
    result = take_duration_offer(state, SERVICES)
    assert result["status"] == "selection_preserved"
    assert result["state"]["selected_service_name"] == "European Facial"
    assert result["state"]["selected_duration_minutes"] == 60
    assert result["offer"] is None


def test_explicitly_accepted_acne_recommendation_replaces_the_pending_service():
    state = initialize_consultation("60-minute European Facial", services=SERVICES)
    state = record_consultation_permission(state, accepted=False)
    state["recommended_service_name"] = "Acne Clarifying Facial"
    state["recommendation_status"] = "accepted"
    result = take_duration_offer(state, SERVICES)
    assert result["status"] == "selection_preserved"
    assert result["state"]["selected_service_name"] == "Acne Clarifying Facial"
    assert result["state"]["selected_duration_minutes"] == 60


def test_time_before_date_is_caller_grounded_and_never_requires_repeated_am_pm():
    result = voice()
    caller_says(result, "2:30 PM.")
    assert result.session.entities["caller_grounded_requested_time"] == "14:30:00"
    assert not result._is_time_grounded("2026-10-14T14:30:00")
    caller_says(result, "Next Wednesday.")
    assert result._is_time_grounded("2026-10-14T14:30:00")
    assert not result._is_time_grounded("2026-10-14T15:00:00")
    assert not result._is_time_grounded("2026-10-15T14:30:00")


@pytest.mark.parametrize("utterance", [
    "Let's switch to the Clarifying Facial.",
    "Let's switch to the Acne Clarifying Facial.",
    "The recommended treatment.",
    "Switch.",
    "Let's switch.",
    "The clarifying facial.",
    "I want the clarifying facial.",
    "I'll go with the recommended treatment.",
    "Yes, I'd like to change.",
    "Yes, that sounds good.",
])
def test_recommendation_accepts_actual_live_caller_wording(utterance):
    from app.services.xai_realtime import XAIVoiceSession

    assert XAIVoiceSession._caller_recommendation_decision(
        utterance,
        recommended_name="Acne Clarifying Facial",
        original_name="European Facial",
    ) == "accepted"


@pytest.mark.parametrize("utterance", [
    "No, let's keep the European Facial.",
    "Keep my original service.",
    "No thanks.",
    "Don't switch.",
    "I want to stay with the European Facial.",
])
def test_recommendation_decline_preserves_original(utterance):
    from app.services.xai_realtime import XAIVoiceSession

    assert XAIVoiceSession._caller_recommendation_decision(
        utterance,
        recommended_name="Acne Clarifying Facial",
        original_name="European Facial",
    ) == "declined"


@pytest.mark.parametrize("utterance", [
    "What's the Clarifying Facial?",
    "How much is the European Facial?",
    "Should I switch to the Clarifying Facial?",
    "Tell me about the Clarifying Facial.",
])
def test_recommendation_information_questions_are_not_consent(utterance):
    from app.services.xai_realtime import XAIVoiceSession

    assert XAIVoiceSession._caller_recommendation_decision(
        utterance,
        recommended_name="Acne Clarifying Facial",
        original_name="European Facial",
    ) is None


@pytest.mark.asyncio
async def test_live_recommendation_questions_then_explicit_switch_continue_to_booking():
    """Actual screenshot sequence: ask about each treatment, then say switch."""
    v = voice()
    await _reach_recommendation(v)
    original = _original_args()
    original["requested_start_iso"] = "2026-10-14T14:30:00"

    caller_says(v, "What's the Clarifying Facial?")
    assert await v._enforce_service_consultation("ask-clarifying", original) is True
    assert v.session.entities["consultation_recommendation_pending"]["facial"]["name"] == "Acne Clarifying Facial"
    assert v._speak_consultation_gate.await_args.kwargs["status"] == "service_recommendation"

    caller_says(v, "How much is the European Facial?")
    assert await v._enforce_service_consultation("ask-price", _original_args()) is True
    assert v.session.entities["consultation_recommendation_pending"]["facial"]["name"] == "Acne Clarifying Facial"

    # This exact wording used to cause repeated questions and no appointment.
    caller_says(v, "Let's switch to the clarifying facial.")
    args = _original_args()
    assert await v._enforce_service_consultation("accept", args) is False
    assert args["requested_services"] == ["Acne Clarifying Facial"]
    assert args["requested_start_iso"] == "2026-10-14T14:30:00"
    assert v.session.entities["consultation_recommendation_pending"] == {}
    assert v.session.entities["consultation_states"]["facial"]["recommendation_status"] == "accepted"
    assert "facial" in v.session.entities["consultation_completed_kinds"]
    assert v.session.entities["consultation_states"]["facial"]["selected_duration_minutes"] == 60
    # No appointment is falsely declared successful merely for selecting a service.
    assert v.session.booking_status not in {"booked", "rescheduled"}

    # Stale model tool arguments cannot resurrect the old service or loop.
    caller_says(v, "The recommended treatment.")
    later = _original_args()
    assert await v._enforce_service_consultation("same", later) is False
    assert later["requested_services"] == ["Acne Clarifying Facial"]


@pytest.mark.asyncio
async def test_bare_switch_advances_without_repeating_recommendation_question():
    v = voice()
    await _reach_recommendation(v)
    prompts = v._speak_consultation_gate.await_count
    caller_says(v, "Switch.")
    args = _original_args()
    assert await v._enforce_service_consultation("switch", args) is False
    assert v._speak_consultation_gate.await_count == prompts
    assert args["requested_services"] == ["Acne Clarifying Facial"]


@pytest.mark.asyncio
async def test_ambiguous_recommendation_answer_is_bounded_and_keeps_original():
    v = voice()
    await _reach_recommendation(v)
    caller_says(v, "Maybe.")
    assert await v._enforce_service_consultation("clarify-once", _original_args()) is True
    assert v._speak_consultation_gate.await_args.kwargs["status"] == "recommendation_clarification"
    caller_says(v, "Hmm.")
    args = _original_args()
    assert await v._enforce_service_consultation("fallback-original", args) is False
    assert args["requested_services"] == ["European Facial"]
    assert v.session.entities["consultation_recommendation_pending"] == {}
    assert v.session.entities["consultation_states"]["facial"]["recommendation_status"] == "declined"


def test_ungrounded_staff_name_from_model_is_removed_not_sent_to_calendar():
    v = voice()
    caller_says(v, "I want to set up a 60-minute European Facial for 2:30 on Wednesday.")
    args = {
        "service_description": "60 minute European facial",
        "requested_start_iso": "2026-10-14T14:30:00",
        "preferred_staff": "SIX",
    }
    v._guard_staff_preference(args)
    assert "preferred_staff" not in args
    assert args["requested_start_iso"] == "2026-10-14T14:30:00"


def test_real_caller_requested_staff_remains_intact():
    v = voice()
    caller_says(v, "I'd like to book my facial with Jordan on Wednesday.")
    args = {"preferred_staff": "Jordan"}
    v._guard_staff_preference(args)
    assert args["preferred_staff"] == "Jordan"
