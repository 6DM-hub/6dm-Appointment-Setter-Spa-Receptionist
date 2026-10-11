"""Regressions for Cara's permission-first facial consultation.

The server owns this flow so model wording cannot skip permission, combine a
list of questions into one turn, or reopen questions the caller already
answered.  Declining the optional questions must still allow an explicit
facial booking to continue.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.services.call_state import CallSession
from app.services.consultation_gate import initialize_consultation, record_consultation_answer
from app.services.xai_realtime import XAIVoiceSession


FACIAL_PERMISSION_PROMPT = (
    "Of course. May I ask you a few quick questions about your skin so I can "
    "relay the details to your esthetician for your appointment?"
)

FACIAL_SERVICES = [
    {
        "name": "30 Minute Custom Facial",
        "duration_minutes": 30,
        "service_family": "Custom Facial",
        "consultation_kind": "facial",
        "consultation_category": "custom",
    },
    {
        "name": "60 Minute Custom Facial",
        "duration_minutes": 60,
        "service_family": "Custom Facial",
        "consultation_kind": "facial",
        "consultation_category": "custom",
    },
    {
        "name": "30 Minute Hydrating Facial",
        "duration_minutes": 30,
        "service_family": "Hydrating Facial",
        "consultation_kind": "facial",
        "consultation_category": "hydrating",
    },
    {
        "name": "60 Minute Hydrating Facial",
        "duration_minutes": 60,
        "service_family": "Hydrating Facial",
        "consultation_kind": "facial",
        "consultation_category": "hydrating",
    },
]


def voice() -> XAIVoiceSession:
    session = CallSession(
        "facial-permission-call",
        "inbound",
        "+15550000002",
        "+15550000001",
        business_name="Test Spa",
        tenant_id="tenant-1",
        timezone="America/Chicago",
    )
    result = XAIVoiceSession(
        "facial-permission-call",
        session,
        now_provider=lambda tz: datetime(2026, 10, 10, 12, 0, tzinfo=tz),
    )
    result._consultation_catalog = AsyncMock(return_value=(FACIAL_SERVICES, []))
    result._speak_consultation_gate = AsyncMock()
    result._send = AsyncMock()
    return result


def caller_says(result: XAIVoiceSession, text: str) -> None:
    result._pending_caller = text
    result._flush_caller_turn()


def spoken(result: XAIVoiceSession) -> str:
    return result._speak_consultation_gate.await_args.kwargs["spoken"]


def facial_args() -> dict:
    return {
        "requested_services": ["60 Minute Custom Facial"],
        "service_description": "60 Minute Custom Facial",
        "requested_start_iso": "2026-10-14T14:00:00-05:00",
    }


@pytest.mark.asyncio
async def test_facial_permission_prompt_is_warm_and_yes_starts_one_question_at_a_time():
    result = voice()
    caller_says(result, "I'd like a 60-minute facial next Wednesday afternoon.")

    assert await result._enforce_service_consultation("permission", facial_args()) is True
    assert spoken(result) == FACIAL_PERMISSION_PROMPT

    caller_says(result, "Yes, that would be helpful.")
    assert await result._enforce_service_consultation("first-question", facial_args()) is True
    first_question = spoken(result)
    assert first_question == "What's the main thing bothering you about your skin right now?"
    assert "midday" not in first_question.casefold()
    assert "breakout" not in first_question.casefold()

    caller_says(result, "Fine lines are my main concern.")
    assert await result._enforce_service_consultation("second-question", facial_args()) is True
    second_question = spoken(result)
    assert "middle of the day" in second_question.casefold()
    assert "end of the day" in second_question.casefold()
    assert "active breakout" not in second_question.casefold()


@pytest.mark.asyncio
async def test_declining_skin_questions_skips_them_without_blocking_explicit_booking():
    result = voice()
    caller_says(result, "Please book a 60-minute custom facial Wednesday at 2 PM.")

    assert await result._enforce_service_consultation("permission", facial_args()) is True
    assert spoken(result) == FACIAL_PERMISSION_PROMPT
    prompt_count = result._speak_consultation_gate.await_count

    caller_says(result, "No thank you, I'd rather just book the facial.")
    continued = await result._enforce_service_consultation("declined", facial_args())

    assert continued is True
    assert result._speak_consultation_gate.await_count == prompt_count + 1
    assert "30-minute" in spoken(result)
    assert "60-minute" in spoken(result)
    assert "skin" not in spoken(result).casefold()
    state = result.session.entities["consultation_states"]["facial"]
    assert state["permission_status"] == "declined"


@pytest.mark.asyncio
async def test_an_active_breakout_already_disclosed_is_not_asked_again():
    result = voice()
    caller_says(result, "I'd like a facial next Wednesday afternoon.")
    assert await result._enforce_service_consultation("permission", facial_args()) is True

    caller_says(result, "Yes, please.")
    assert await result._enforce_service_consultation("main-concern", facial_args()) is True
    assert "main thing bothering" in spoken(result).casefold()

    caller_says(result, "Active breakouts and clogged pores are my main concern.")
    assert await result._enforce_service_consultation("skin-feel", facial_args()) is True
    assert "middle of the day" in spoken(result).casefold()

    caller_says(result, "It gets oily by the middle of the day.")
    assert await result._enforce_service_consultation("after-skin-feel", facial_args()) is True
    next_question = spoken(result).casefold()
    assert "active breakout" not in next_question
    assert "sensitivity" in next_question
    assert "products" in next_question


@pytest.mark.asyncio
async def test_permission_yes_with_skin_details_credits_them_immediately():
    result = voice()
    caller_says(result, "I'd like a facial next Wednesday afternoon.")
    assert await result._enforce_service_consultation("permission", facial_args()) is True

    caller_says(
        result,
        "Yes, please. My skin is dry and I have active breakouts.",
    )
    assert await result._enforce_service_consultation("details-with-yes", facial_args()) is True

    question = spoken(result).casefold()
    assert "main thing bothering" not in question
    assert "middle of the day" not in question
    assert "active breakouts" not in question
    assert "sensitivity" in question


@pytest.mark.asyncio
async def test_permission_yes_is_not_overridden_by_a_negative_skin_fact():
    result = voice()
    caller_says(result, "I'd like a facial next Wednesday afternoon.")
    assert await result._enforce_service_consultation("permission", facial_args()) is True

    caller_says(result, "Yes, please. I have no active breakouts or redness.")
    assert await result._enforce_service_consultation("accepted", facial_args()) is True

    state = result.session.entities["consultation_states"]["facial"]
    assert state["permission_status"] == "accepted"
    assert state["facial_summary"]["active_breakouts"] == "not reported"
    assert state["facial_summary"]["sensitivity"] == "not reported"
    assert "active breakout" not in spoken(result).casefold()


@pytest.mark.asyncio
async def test_yes_prefixed_question_does_not_count_as_permission():
    result = voice()
    caller_says(result, "I'd like a facial next Wednesday afternoon.")
    assert await result._enforce_service_consultation("permission", facial_args()) is True

    caller_says(result, "Yes, but what are you going to ask?")
    assert await result._enforce_service_consultation("clarify-permission", facial_args()) is True

    assert "would you like to answer" in spoken(result).casefold()
    state = result.session.entities["consultation_states"]["facial"]
    assert state["permission_status"] is None


@pytest.mark.asyncio
async def test_repeat_request_gets_one_spoken_clarification_instead_of_silence():
    result = voice()
    caller_says(result, "I'd like a facial next Wednesday afternoon.")
    assert await result._enforce_service_consultation("permission", facial_args()) is True

    caller_says(result, "Yes, please.")
    assert await result._enforce_service_consultation("main-concern", facial_args()) is True
    assert "main thing bothering" in spoken(result).casefold()

    caller_says(result, "Could you repeat that?")
    assert await result._enforce_service_consultation("clarify", facial_args()) is True
    assert spoken(result) == "What would you most like your esthetician to focus on?"
    state = result.session.entities["consultation_states"]["facial"]
    assert "main_concern" not in state["answered_fields"]


@pytest.mark.asyncio
async def test_two_unclear_answers_move_forward_without_inventing_skin_details():
    result = voice()
    caller_says(result, "I'd like a facial next Wednesday afternoon.")
    assert await result._enforce_service_consultation("permission", facial_args()) is True

    caller_says(result, "Yes, please.")
    assert await result._enforce_service_consultation("main-concern", facial_args()) is True

    caller_says(result, "Could you repeat that?")
    assert await result._enforce_service_consultation("clarify", facial_args()) is True

    caller_says(result, "I'm not sure.")
    assert await result._enforce_service_consultation("continue", facial_args()) is True
    assert "middle of the day" in spoken(result).casefold()
    state = result.session.entities["consultation_states"]["facial"]
    assert "main_concern" in state["answered_fields"]
    assert state["facial_summary"]["concerns"] == []


@pytest.mark.asyncio
async def test_unsure_answer_moves_to_next_question_without_repeating():
    result = voice()
    caller_says(result, "I'd like a facial next Wednesday afternoon.")
    assert await result._enforce_service_consultation("permission", facial_args()) is True

    caller_says(result, "Yes, please.")
    assert await result._enforce_service_consultation("main-concern", facial_args()) is True

    caller_says(result, "I'm not sure.")
    assert await result._enforce_service_consultation("unsure", facial_args()) is True
    assert "middle of the day" in spoken(result).casefold()
    state = result.session.entities["consultation_states"]["facial"]
    assert state["facial_summary"]["concerns"] == []


@pytest.mark.asyncio
async def test_caller_can_skip_remaining_questions_and_continue_booking():
    result = voice()
    caller_says(result, "I'd like a 60-minute facial next Wednesday afternoon.")
    assert await result._enforce_service_consultation("permission", facial_args()) is True

    caller_says(result, "Yes, please.")
    assert await result._enforce_service_consultation("main-concern", facial_args()) is True

    caller_says(result, "Let's skip the questions and just continue booking.")
    assert await result._enforce_service_consultation("skip", facial_args()) is True

    assert "30-minute" in spoken(result)
    assert "60-minute" in spoken(result)
    assert "skin" not in spoken(result).casefold()
    state = result.session.entities["consultation_states"]["facial"]
    assert state["permission_status"] == "declined"


@pytest.mark.asyncio
async def test_persisted_answers_are_skipped_instead_of_repeated():
    result = voice()
    state = initialize_consultation(
        "60 Minute Custom Facial",
        services=FACIAL_SERVICES,
    )
    assert state is not None
    state["permission_status"] = "accepted"
    state = record_consultation_answer(
        state,
        field="main_concern",
        answer="Dry and dull skin",
    )
    state = record_consultation_answer(
        state,
        field="skin_feel",
        answer="Dry and tight by midday",
    )
    result.session.entities["consultation_states"] = {"facial": state}
    caller_says(result, "I'd like to keep going with that facial.")

    assert await result._enforce_service_consultation("resume", facial_args()) is True
    question = spoken(result).casefold()
    assert "main thing bothering" not in question
    assert "middle of the day" not in question
    assert "active breakout" in question


@pytest.mark.asyncio
async def test_esthetician_summary_and_grounded_recommendation_precede_availability():
    result = voice()
    caller_says(result, "I'd like a facial next Wednesday afternoon.")
    args = {
        "requested_services": ["facial"],
        "service_description": "facial",
        "requested_start_iso": "2026-10-14T14:00:00-05:00",
    }

    assert await result._enforce_service_consultation("permission", args) is True
    answers = (
        "Yes, please.",
        "Fine lines and uneven tone are my main concern.",
        "It feels dry and tight by the middle of the day.",
        "No active breakouts.",
        "No sensitivity or redness, and products do not usually irritate it.",
        "I've had facials before and liked gentle hydration, but not harsh exfoliation.",
    )
    for index, answer in enumerate(answers, start=1):
        caller_says(result, answer)
        consumed = await result._enforce_service_consultation(f"answer-{index}", args)
        assert consumed is True

    recommendation = spoken(result)
    assert "esthetician" in recommendation.casefold()
    assert "Hydrating Facial" in recommendation
    assert "30-minute" in recommendation
    assert "60-minute" in recommendation
    assert "30-minute 30 Minute" not in recommendation
    assert "60-minute 60 Minute" not in recommendation
    assert "Would you prefer the 60-minute or the 30-minute?" in recommendation
    assert result.session.entities.get("consultation_completed_kinds") in (None, [])

