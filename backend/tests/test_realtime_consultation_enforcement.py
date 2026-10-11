from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from app.services.booking_state import start_new_intent
from app.services.call_state import CallSession
from app.services.consultation_gate import (
    initialize_consultation,
    record_consultation_answer,
    record_consultation_permission,
    record_duration_selection,
    take_duration_offer,
    take_massage_addon_offer,
)
from app.services.xai_realtime import XAIVoiceSession


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

FACIAL_SERVICES = [
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
]


CONSULTATIVE_SERVICES = [
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
    *MASSAGE_SERVICES,
    {
        "name": "30 Minute Sports Massage",
        "duration_minutes": 30,
        "service_family": "Sports Massage",
        "consultation_kind": "massage",
        "consultation_category": "deeper_pressure_sports",
    },
    {
        "name": "60 Minute Sports Massage",
        "duration_minutes": 60,
        "service_family": "Sports Massage",
        "consultation_kind": "massage",
        "consultation_category": "deeper_pressure_sports",
    },
    {
        "name": "Hot Stones",
        "duration_minutes": 15,
        "is_add_on": True,
    },
    {
        "name": "CBD Oil",
        "duration_minutes": 5,
        "is_add_on": True,
    },
]


MASSAGE_UPSELL_RULES = [
    {
        "base_service": "60 Minute Swedish Massage",
        "allowed_upsells": ["Hot Stones", "CBD Oil"],
    }
]


def voice() -> XAIVoiceSession:
    session = CallSession(
        "consultation-call",
        "inbound",
        "+15550000002",
        "+15550000001",
        business_name="Test Spa",
        tenant_id="tenant-1",
        timezone="America/Chicago",
    )
    result = XAIVoiceSession(
        "consultation-call",
        session,
        now_provider=lambda tz: datetime(2026, 10, 10, 12, 0, tzinfo=tz),
    )
    result._speak_consultation_gate = AsyncMock()
    result._send = AsyncMock()
    return result


def caller_says(result: XAIVoiceSession, text: str) -> None:
    result._pending_caller = text
    result._flush_caller_turn()


def completed_massage_addon_state() -> dict:
    state = initialize_consultation(
        "60 Minute Swedish Massage", services=CONSULTATIVE_SERVICES
    )
    assert state is not None
    for field, answer in (
        ("massage_reason", "deep tension and recovery"),
        ("massage_areas", "upper back"),
        ("pressure_preference", "deep"),
        ("safety_answered", "none"),
    ):
        state = record_consultation_answer(state, field=field, answer=answer)
    state = take_duration_offer(state, CONSULTATIVE_SERVICES)["state"]
    state = take_massage_addon_offer(
        state, CONSULTATIVE_SERVICES, MASSAGE_UPSELL_RULES
    )["state"]
    return state


@pytest.mark.asyncio
async def test_massage_consultation_is_enforced_and_original_request_survives():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(MASSAGE_SERVICES, []))
    caller_says(
        result,
        "I need a 60-minute Swedish massage with SIX next Wednesday afternoon.",
    )
    args = {
        "requested_services": ["60 Minute Swedish Massage"],
        "service_description": "60 Minute Swedish Massage",
        "preferred_staff": "SIX",
    }

    assert await result._enforce_service_consultation("tool-1", args) is True
    assert result._speak_consultation_gate.await_args.kwargs["spoken"].startswith(
        "What's the main reason"
    )

    for text, expected in (
        ("Mostly relaxation.", "particular areas"),
        ("My shoulders.", "lighter, medium, or deeper"),
        ("Medium pressure.", "injuries or areas"),
    ):
        caller_says(result, text)
        partial = {"requested_services": ["Swedish Massage"]}
        assert await result._enforce_service_consultation("tool-next", partial) is True
        assert expected in result._speak_consultation_gate.await_args.kwargs["spoken"]

    caller_says(result, "No injuries or areas to avoid.")
    partial = {"requested_services": ["Swedish Massage"]}
    assert await result._enforce_service_consultation("tool-duration", partial) is True
    duration_line = result._speak_consultation_gate.await_args.kwargs["spoken"]
    assert "30-minute" in duration_line
    assert "60-minute" in duration_line

    caller_says(result, "Keep the 60-minute option.")
    final_args = {"requested_services": ["Swedish Massage"]}
    assert await result._enforce_service_consultation("tool-final", final_args) is False
    assert final_args["requested_services"] == ["60 Minute Swedish Massage"]
    assert final_args["preferred_staff"] == "SIX"
    window = result._spoken_day_part_window()
    assert window is not None
    assert window[0].date().isoformat() == "2026-10-14"
    assert (window[0].hour, window[1].hour) == (12, 17)


@pytest.mark.asyncio
async def test_facial_questions_and_soft_duration_offer_cannot_be_skipped():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(FACIAL_SERVICES, []))
    caller_says(result, "I'd like a 30-minute facial next Wednesday morning.")
    args = {"requested_services": ["Petite Facial"]}

    expected_questions = (
        "main thing bothering you",
        "middle of the day",
        "active breakouts",
        "sensitivity or redness",
        "had facials before",
    )
    assert await result._enforce_service_consultation("f-1", args) is True
    assert "quick questions about your skin" in result._speak_consultation_gate.await_args.kwargs["spoken"]

    caller_says(result, "Yes, please.")
    assert await result._enforce_service_consultation("f-permission", args) is True
    assert expected_questions[0] in result._speak_consultation_gate.await_args.kwargs["spoken"]
    for answer, expected in (
        ("Fine lines are my main concern.", expected_questions[1]),
        ("Usually dry.", expected_questions[2]),
        ("No active breakouts.", expected_questions[3]),
        ("No sensitivity, redness, or irritating products.", expected_questions[4]),
    ):
        caller_says(result, answer)
        assert await result._enforce_service_consultation(
            "f-next", {"requested_services": ["Petite Facial"]}
        ) is True
        assert expected in result._speak_consultation_gate.await_args.kwargs["spoken"]

    caller_says(result, "I've had facials before and like gentle hydration.")
    assert await result._enforce_service_consultation(
        "f-duration", {"requested_services": ["Petite Facial"]}
    ) is True
    offer = result._speak_consultation_gate.await_args.kwargs["spoken"]
    assert "focused start" in offer
    assert "more time to target your concern" in offer
    assert "Would you prefer the 60-minute or the 30-minute?" in offer

    caller_says(result, "Stay with 30 minutes.")
    final_args = {"requested_services": ["facial"]}
    assert await result._enforce_service_consultation("f-final", final_args) is False
    assert final_args["requested_services"] == ["Petite Facial"]


@pytest.mark.asyncio
async def test_same_turn_duplicate_tool_call_does_not_repeat_consultation_question():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(FACIAL_SERVICES, []))
    caller_says(result, "I want a facial tomorrow afternoon.")
    args = {"requested_services": ["facial"]}

    assert await result._enforce_service_consultation("f-1", args) is True
    assert await result._enforce_service_consultation("f-duplicate", args) is True
    assert result._speak_consultation_gate.await_count == 1


@pytest.mark.asyncio
async def test_partial_tool_call_does_not_drop_other_requested_modality():
    result = voice()
    result._consultation_catalog = AsyncMock(
        return_value=(CONSULTATIVE_SERVICES, MASSAGE_UPSELL_RULES)
    )
    both_services = ["60 Minute Swedish Massage", "30 Minute Custom Facial"]
    caller_says(result, "I'd like a massage and a facial next Wednesday.")

    assert await result._enforce_service_consultation(
        "both-1", {"requested_services": both_services}
    ) is True

    # Realtime models commonly send only the service tied to the question they
    # are answering. That partial tool call must not erase the other service.
    caller_says(result, "Mostly relaxation.")
    assert await result._enforce_service_consultation(
        "both-2", {"requested_services": ["Swedish Massage"]}
    ) is True

    remembered = result.session.entities["consultation_booking_request"]
    assert remembered["requested_services"] == both_services


def test_new_booking_intent_clears_every_consultation_progress_key():
    result = voice()
    consultation_keys = {
        "consultation_state": {"kind": "massage"},
        "consultation_states": {"massage": {"kind": "massage"}},
        "consultation_completed_kinds": ["massage"],
        "consultation_question_turns": {"facial": 3},
        "consultation_answer_attempts": {"facial": 1},
        "consultation_permission_turns": {"facial": 2},
        "consultation_permission_attempts": {"facial": 1},
        "consultation_duration_offer_turns": {"massage": 5},
        "consultation_duration_attempts": {"massage": 1},
        "consultation_addon_pending": {
            "massage": {"turn": 6, "names": ["Hot Stones"]}
        },
        "consultation_booking_request": {
            "requested_services": ["60 Minute Swedish Massage"]
        },
        "consultation_addon_names": ["Hot Stones"],
    }
    result.session.entities.update(consultation_keys)

    start_new_intent(result.session)

    assert not (set(consultation_keys) & set(result.session.entities))


@pytest.mark.asyncio
async def test_duration_offer_duplicate_waits_silently_and_selection_survives_tool_name_change():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(FACIAL_SERVICES, []))
    state = initialize_consultation("facial", services=FACIAL_SERVICES)
    assert state is not None
    state = record_consultation_permission(state, accepted=False)
    state = take_duration_offer(state, FACIAL_SERVICES)["state"]
    caller_says(result, "I would like a facial.")
    result.session.entities.update(
        {
            "consultation_state": state,
            "consultation_states": {"facial": state},
            "consultation_duration_offer_turns": {"facial": result._user_turn_count},
            "consultation_booking_request": {"requested_services": ["facial"]},
        }
    )

    # A second tool call in the same caller turn must not replay the offer or
    # let the model's catalog name reset the server-owned pending choices.
    args = {"requested_services": ["European Facial"]}
    assert await result._enforce_service_consultation("same-turn", args) is True
    assert result._speak_consultation_gate.await_count == 0
    state = result.session.entities["consultation_states"]["facial"]
    assert state["duration_offer_presented"] is True
    assert len(state["duration_choices"]) == 2

    caller_says(result, "The 60-minute option, please.")
    args = {"requested_services": ["European Facial"]}
    assert await result._enforce_service_consultation("duration-answer", args) is False
    assert args["requested_services"] == ["European Facial"]
    state = result.session.entities["consultation_states"]["facial"]
    assert state["selected_duration_minutes"] == 60
    assert state["selected_service_name"] == "European Facial"
    assert result.session.entities["consultation_completed_kinds"] == ["facial"]


@pytest.mark.asyncio
async def test_duration_clarification_keeps_pending_latch_for_the_next_valid_answer():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(FACIAL_SERVICES, []))
    state = initialize_consultation("facial", services=FACIAL_SERVICES)
    assert state is not None
    state = record_consultation_permission(state, accepted=False)
    state = take_duration_offer(state, FACIAL_SERVICES)["state"]
    caller_says(result, "I would like a facial.")
    result.session.entities.update(
        {
            "consultation_state": state,
            "consultation_states": {"facial": state},
            "consultation_duration_offer_turns": {"facial": result._user_turn_count},
            "consultation_booking_request": {"requested_services": ["facial"]},
        }
    )

    caller_says(result, "How much do the options cost?")
    assert await result._enforce_service_consultation(
        "duration-clarification", {"requested_services": ["European Facial"]}
    ) is True
    assert result.session.entities["consultation_duration_offer_turns"]["facial"] == (
        result._user_turn_count
    )

    caller_says(result, "Sixty minutes, please.")
    args = {"requested_services": ["European Facial"]}
    assert await result._enforce_service_consultation("duration-final", args) is False
    assert args["requested_services"] == ["European Facial"]
    assert result.session.entities["consultation_states"]["facial"][
        "selected_duration_minutes"
    ] == 60


@pytest.mark.asyncio
async def test_completed_facial_choice_ignores_stale_tool_service_and_clock_minutes():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(FACIAL_SERVICES, []))
    state = initialize_consultation("facial", services=FACIAL_SERVICES)
    assert state is not None
    state = record_consultation_permission(state, accepted=False)
    state = take_duration_offer(state, FACIAL_SERVICES)["state"]
    state = record_duration_selection(state, selected_duration_minutes=60)
    result.session.entities.update(
        {
            "consultation_state": state,
            "consultation_states": {"facial": state},
            "consultation_completed_kinds": ["facial"],
            "consultation_booking_request": {
                "requested_services": ["European Facial"],
                "service_description": "European Facial",
            },
        }
    )

    caller_says(result, "Two thirty PM works for me.")
    args = {"requested_services": ["Petite Facial"]}
    assert await result._enforce_service_consultation("stale-service", args) is False
    assert result._speak_consultation_gate.await_count == 0
    assert args["requested_services"] == ["European Facial"]
    remembered = result.session.entities["consultation_booking_request"]
    assert remembered["requested_services"] == ["European Facial"]
    state = result.session.entities["consultation_states"]["facial"]
    assert state["selected_duration_minutes"] == 60


@pytest.mark.asyncio
async def test_completed_facial_allows_an_explicit_caller_duration_change():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(FACIAL_SERVICES, []))
    state = initialize_consultation("facial", services=FACIAL_SERVICES)
    assert state is not None
    state = record_consultation_permission(state, accepted=False)
    state = take_duration_offer(state, FACIAL_SERVICES)["state"]
    state = record_duration_selection(state, selected_duration_minutes=60)
    result.session.entities.update(
        {
            "consultation_state": state,
            "consultation_states": {"facial": state},
            "consultation_completed_kinds": ["facial"],
            "consultation_booking_request": {
                "requested_services": ["European Facial"]
            },
        }
    )

    caller_says(result, "Actually, change that to the 30-minute facial.")
    args = {"requested_services": ["Petite Facial"]}
    assert await result._enforce_service_consultation("caller-change", args) is False
    assert args["requested_services"] == ["Petite Facial"]
    remembered = result.session.entities["consultation_booking_request"]
    assert remembered["requested_services"] == ["Petite Facial"]
    state = result.session.entities["consultation_states"]["facial"]
    assert state["selected_duration_minutes"] == 30


@pytest.mark.asyncio
async def test_completed_multi_service_duration_change_is_scoped_to_named_modality():
    result = voice()
    result._consultation_catalog = AsyncMock(
        return_value=(CONSULTATIVE_SERVICES, MASSAGE_UPSELL_RULES)
    )
    facial = initialize_consultation("facial", services=CONSULTATIVE_SERVICES)
    assert facial is not None
    facial = record_consultation_permission(facial, accepted=False)
    facial = take_duration_offer(facial, CONSULTATIVE_SERVICES)["state"]
    facial = record_duration_selection(facial, selected_duration_minutes=60)
    massage = completed_massage_addon_state()
    result.session.entities.update(
        {
            "consultation_state": facial,
            "consultation_states": {"facial": facial, "massage": massage},
            "consultation_completed_kinds": ["facial", "massage"],
            "consultation_booking_request": {
                "requested_services": [
                    "60 Minute Custom Facial",
                    "60 Minute Swedish Massage",
                ]
            },
        }
    )

    caller_says(result, "Actually, change that to the 30-minute facial.")
    args = {
        "requested_services": [
            "30 Minute Custom Facial",
            "60 Minute Swedish Massage",
        ]
    }
    assert await result._enforce_service_consultation("scoped-change", args) is False
    assert args["requested_services"] == [
        "30 Minute Custom Facial",
        "60 Minute Swedish Massage",
    ]
    states = result.session.entities["consultation_states"]
    assert states["facial"]["selected_duration_minutes"] == 30
    assert states["massage"]["selected_duration_minutes"] == 60


@pytest.mark.asyncio
async def test_generic_facial_answers_drive_catalog_family_recommendation():
    result = voice()
    result._consultation_catalog = AsyncMock(return_value=(CONSULTATIVE_SERVICES, []))
    caller_says(result, "I'd like a facial next Wednesday.")

    assert await result._enforce_service_consultation(
        "generic-facial-1", {"requested_services": ["facial"]}
    ) is True
    for index, answer in enumerate(
        (
            "Yes, please.",
            "Dullness is my main concern.",
            "Dry by midday.",
            "No active breakouts.",
            "No sensitivity, redness, or irritating products.",
            "I've had facials and liked gentle hydration.",
        ),
        start=2,
    ):
        caller_says(result, answer)
        assert await result._enforce_service_consultation(
            f"generic-facial-{index}", {"requested_services": ["facial"]}
        ) is True

    state = result.session.entities["consultation_states"]["facial"]
    assert state["category"] == "hydrating"
    assert [choice["service_name"] for choice in state["duration_choices"]] == [
        "30 Minute Hydrating Facial",
        "60 Minute Hydrating Facial",
    ]
    assert "Hydrating Facial" in result._speak_consultation_gate.await_args.kwargs[
        "spoken"
    ]


@pytest.mark.asyncio
async def test_generic_massage_answers_drive_catalog_family_recommendation():
    result = voice()
    result._consultation_catalog = AsyncMock(
        return_value=(CONSULTATIVE_SERVICES, MASSAGE_UPSELL_RULES)
    )
    caller_says(result, "I'd like a massage next Wednesday.")

    assert await result._enforce_service_consultation(
        "generic-massage-1", {"requested_services": ["massage"]}
    ) is True
    for index, answer in enumerate(
        ("Workout recovery.", "Mostly my legs.", "Deep pressure.", "No injuries."),
        start=2,
    ):
        caller_says(result, answer)
        assert await result._enforce_service_consultation(
            f"generic-massage-{index}", {"requested_services": ["massage"]}
        ) is True

    state = result.session.entities["consultation_states"]["massage"]
    assert state["category"] == "deeper_pressure_sports"
    assert [choice["service_name"] for choice in state["duration_choices"]] == [
        "30 Minute Sports Massage",
        "60 Minute Sports Massage",
    ]
    assert "Sports Massage" in result._speak_consultation_gate.await_args.kwargs[
        "spoken"
    ]


@pytest.mark.asyncio
async def test_accepted_massage_addon_survives_while_facial_questions_continue():
    result = voice()
    result._consultation_catalog = AsyncMock(
        return_value=(CONSULTATIVE_SERVICES, MASSAGE_UPSELL_RULES)
    )
    both_services = ["60 Minute Swedish Massage", "30 Minute Custom Facial"]
    result.session.entities.update(
        {
            "consultation_states": {
                "massage": completed_massage_addon_state(),
            },
            "consultation_addon_pending": {
                "massage": {"turn": 0, "names": ["Hot Stones", "CBD Oil"]}
            },
            "consultation_booking_request": {
                "requested_services": list(both_services)
            },
        }
    )
    caller_says(result, "Hot Stones, please.")
    args = {"requested_services": list(both_services)}

    # The facial consultation legitimately consumes this turn, but it cannot
    # discard the add-on the caller just accepted for the massage.
    assert await result._enforce_service_consultation("cross-service", args) is True
    assert "Hot Stones" in args["requested_services"]
    assert "Hot Stones" in result.session.entities["consultation_booking_request"][
        "requested_services"
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "expected_addons"),
    [
        ("the first one", ["Hot Stones"]),
        ("the second one", ["CBD Oil"]),
        ("both, please", ["Hot Stones", "CBD Oil"]),
    ],
)
async def test_addon_reply_understands_ordinal_and_both(reply, expected_addons):
    result = voice()
    result._consultation_catalog = AsyncMock(
        return_value=(CONSULTATIVE_SERVICES, MASSAGE_UPSELL_RULES)
    )
    result.session.entities.update(
        {
            "consultation_states": {
                "massage": completed_massage_addon_state(),
            },
            "consultation_addon_pending": {
                "massage": {"turn": 0, "names": ["Hot Stones", "CBD Oil"]}
            },
            "consultation_booking_request": {
                "requested_services": ["60 Minute Swedish Massage"]
            },
        }
    )
    caller_says(result, reply)
    args = {"requested_services": ["60 Minute Swedish Massage"]}

    assert await result._enforce_service_consultation("addon-choice", args) is False
    assert args["requested_services"] == [
        "60 Minute Swedish Massage",
        *expected_addons,
    ]

