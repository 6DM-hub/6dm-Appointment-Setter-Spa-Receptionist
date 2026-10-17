"""Cara must not re-ask a consultation question the caller already answered.

Replays the Rubyz test call of 2026-10-14 (11:53 AM CDT):
  "Are you dealing with any active breakouts right now?" -> "Yes, on my forehead."
  was asked again; "Would you like the 60-minute treatment?" -> "Yes." was asked
  again, and a second "Yes." ended in a staff-callback offer.
"""
from unittest.mock import AsyncMock

import pytest

from app.services.consultation_gate import _facial_answer_recognized
from tests.test_realtime_consultation_enforcement import caller_says, voice

# One facial family offered in a single length, like Rubyz's menu.
SINGLE_LENGTH_FACIALS = [
    {
        "name": "Deep Cleansing Pore Refining Facial",
        "duration_minutes": 60,
        "service_family": "Deep Cleansing Pore Refining Facial",
        "consultation_kind": "facial",
        "consultation_category": "acne_clarifying",
    },
]


@pytest.mark.parametrize("answer", [
    "Yes, on my forehead.",
    "Yeah, a few on my chin",
    "No, not right now.",
    "Nope, my skin is clear",
    "I don't",
    "I haven't",
])
def test_breakout_answers_with_extra_words_are_recognized(answer):
    assert _facial_answer_recognized("active_breakouts", answer)


@pytest.mark.parametrize("answer", ["Yes, a little around my nose.", "No, not really.", "I don't."])
def test_sensitivity_answers_with_extra_words_are_recognized(answer):
    assert _facial_answer_recognized("skin_sensitivity", answer)


@pytest.mark.parametrize("answer", ["What?", "Can you repeat the question?", "sorry", "I'm not sure"])
def test_non_answers_still_are_not_treated_as_answers(answer):
    assert not _facial_answer_recognized("active_breakouts", answer)


def _spoken(result):
    return [call.kwargs.get("spoken") for call in result._speak_consultation_gate.await_args_list]


def _statuses(result):
    return [call.kwargs.get("status") for call in result._speak_consultation_gate.await_args_list]


@pytest.mark.asyncio
async def test_rubyz_call_replay_asks_each_question_once_and_accepts_yes_to_the_only_length():
    result = voice()
    result._consultation_catalog = AsyncMock(
        return_value=(SINGLE_LENGTH_FACIALS, [])
    )
    result._offer_staff_callback = AsyncMock()
    args = {"requested_services": ["facial"]}

    caller_says(result, "Yeah, I want to set up a facial.")
    assert await result._enforce_service_consultation("t1", args) is True
    for index, answer in enumerate(
        (
            "Yes.",                               # permission for skin questions
            "Oily and breaking out.",             # main concern
            "Yes, on my forehead.",               # active breakouts (was re-asked)
            "No.",                                # sensitivity
            "The decollete massage I loved.",     # facial history
        ),
        start=2,
    ):
        caller_says(result, answer)
        assert await result._enforce_service_consultation(f"t{index}", args) is True

    statuses = _statuses(result)
    assert "consultation_answer_clarification" not in statuses
    assert not any("yes, no, or are you unsure" in (text or "") for text in _spoken(result))
    assert statuses[-1] == "duration_options"
    assert "Would you like the 60-minute treatment?" in _spoken(result)[-1]

    asked_before = len(statuses)
    caller_says(result, "Yes.")
    await result._enforce_service_consultation("t7", args)

    state = result.session.entities["consultation_states"]["facial"]
    assert state["selected_duration_minutes"] == 60
    new_statuses = _statuses(result)[asked_before:]
    assert "duration_selection_required" not in new_statuses
    assert not any("prefer the 60-minute option" in (text or "") for text in _spoken(result))
    result._offer_staff_callback.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_to_the_only_length_does_not_select_it():
    result = voice()
    result._consultation_catalog = AsyncMock(
        return_value=(SINGLE_LENGTH_FACIALS, [])
    )
    result._offer_staff_callback = AsyncMock()
    args = {"requested_services": ["facial"]}
    caller_says(result, "I want a facial.")
    await result._enforce_service_consultation("n1", args)
    for index, answer in enumerate(
        ("Yes.", "Oily skin.", "No.", "Nope.", "First facial."), start=2
    ):
        caller_says(result, answer)
        await result._enforce_service_consultation(f"n{index}", args)
    assert _statuses(result)[-1] == "duration_options"
    caller_says(result, "No.")
    await result._enforce_service_consultation("n7", args)
    # A "no" is not a choice: Cara asks again instead of booking the 60 minutes.
    assert _statuses(result)[-1] == "duration_selection_required"
