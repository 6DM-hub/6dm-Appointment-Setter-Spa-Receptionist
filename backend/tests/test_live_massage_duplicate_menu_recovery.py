"""Reproduce the 2026-10-11 failed live massage call.

Real spa/Square menus frequently have two variations named "Swedish Massage",
distinguished by duration_minutes=60 or 90, unlike the old unit fixtures
which spelled the duration into each service name.

The caller's initial duration reply ("Let's do a 60-minute") arrived BEFORE
four consultation answers and the subsequent model tool calls omitted date and
time. Neither that sequence nor a duplicate menu name may force a second
duration offer or block availability with an irrelevant callback.
"""
from unittest.mock import AsyncMock

import pytest

from app.services.consultation_gate import (
    availability_gate,
    initialize_consultation,
    record_consultation_answer,
    record_pending_answer,
    record_duration_selection,
    take_next_question,
    take_duration_offer,
    take_massage_addon_offer,
)
from tests.test_realtime_consultation_enforcement import caller_says, voice
from tests.test_swedish_catalog_order import MENU as SQUARE_CATEGORY_FIRST_MENU


DUPLICATE_NAME_MENU = [
    {
        "name": "Swedish Massage",
        "duration_minutes": 60,
        "service_family": "Swedish Massage",
        "consultation_kind": "massage",
    },
    {
        "name": "Swedish Massage",
        "duration_minutes": 90,
        "service_family": "Swedish Massage",
        "consultation_kind": "massage",
    },
    {
        "name": "Sports Massage",
        "duration_minutes": 60,
        "service_family": "Sports Massage",
        "consultation_kind": "massage",
    },
]


def _complete_massage(service_name="Swedish Massage", minutes=None):
    state = initialize_consultation(
        service_name,
        services=DUPLICATE_NAME_MENU,
        requested_duration_minutes=minutes,
    )
    assert state is not None
    for field, answer in (
        ("massage_reason", "sore back and legs"),
        ("massage_areas", "back and legs"),
        ("pressure_preference", "medium to deep"),
        ("safety_answered", "no"),
    ):
        state = record_consultation_answer(state, field=field, answer=answer)
    return state


def test_caller_selected_duplicate_menu_label_resolves_without_offer():
    state = _complete_massage("60 minute Swedish Massage")
    res = take_duration_offer(state, DUPLICATE_NAME_MENU)
    assert res["status"] == "selection_preserved"
    assert res["offer"] is None
    assert res["state"]["selected_duration_minutes"] == 60
    # The selection must remain variation-qualified for the Square adapter.
    assert res["state"]["selected_service_name"] == "60 minute Swedish Massage"
    addons = take_massage_addon_offer(res["state"], DUPLICATE_NAME_MENU, [])
    assert addons["status"] == "no_relevant_addons"
    assert addons["state"]["addon_offer_presented"] is True
    assert availability_gate(addons["state"]) == {"allow": True, "status": "ready"}


def test_prior_swedish_variant_without_duration_needs_actual_caller_selection():
    state = _complete_massage("Swedish Massage")
    res = take_duration_offer(state, DUPLICATE_NAME_MENU)
    assert res["status"] == "offer_duration"
    assert [v["minutes"] for v in res["state"]["duration_choices"]] == [60, 90]
    chosen = record_duration_selection(res["state"], selected_duration_minutes=60)
    assert chosen["selected_service_name"] == "60 minute Swedish Massage"
    addon = take_massage_addon_offer(chosen, DUPLICATE_NAME_MENU, [])
    assert addon["status"] == "no_relevant_addons"
    assert availability_gate(addon["state"])["allow"] is True


def test_explicit_grounded_selection_skips_duration_comparison_for_ambiguous_label():
    state = _complete_massage("Swedish Massage", minutes=60)
    res = take_duration_offer(
        state, DUPLICATE_NAME_MENU, requested_duration_grounded=True
    )
    assert res["status"] == "selection_preserved"
    assert res["offer"] is None
    assert res["state"]["selected_service_name"] == "60 minute Swedish Massage"
    assert availability_gate(
        take_massage_addon_offer(res["state"], DUPLICATE_NAME_MENU, [])["state"]
    )["allow"] is True


def test_no_model_only_duration_selected_without_caller_grounding():
    state = _complete_massage("Swedish Massage", minutes=60)
    res = take_duration_offer(state, DUPLICATE_NAME_MENU)
    # A model field alone is not a customer-confirmed choice for ambiguous
    # duplicate-named variants. Offer the actual menu choices instead.
    assert res["status"] == "offer_duration"
    assert [item["minutes"] for item in res["state"]["duration_choices"]] == [60, 90]
    assert res["state"]["selected_service_name"] == "60 minute Swedish Massage"


def test_name_normalization_does_not_erase_answered_consultation():
    state = _complete_massage("Swedish Massage", minutes=60)
    later = initialize_consultation(
        "60-minute Swedish Massage", services=DUPLICATE_NAME_MENU,
        prior_state=state,
    )
    assert later is not None
    assert len(later["answered_fields"]) == 4
    assert later["requested_duration_minutes"] == 60
    assert not later["duration_offer_presented"]


@pytest.mark.asyncio
async def test_exact_live_massage_call_skips_reask_and_reaches_availability():
    v = voice()
    v._consultation_catalog = AsyncMock(return_value=(DUPLICATE_NAME_MENU, []))
    caller_says(v, "Let's book a Swedish massage.")
    caller_says(v, "Let's do a 60-minute.")
    assert v._caller_grounded_consultation_duration(
        "massage", "Swedish Massage", DUPLICATE_NAME_MENU
    ) == 60

    # The first tool call comes only AFTER the caller already selected a
    # 60-minute Swedish Massage. The model loses the exact date/time in args.
    args = {"service_description": "60 minute Swedish massage", "earliest": True}
    assert await v._enforce_service_consultation("reason", args) is True
    assert "main reason" in v._speak_consultation_gate.await_args.kwargs["spoken"]

    for utterance, expected in (
        ("My back and legs are sore.", "pressure"),
        ("Medium to deep.", "injuries"),
    ):
        caller_says(v, utterance)
        args = {"service_description": "60 minute Swedish massage", "earliest": True}
        assert await v._enforce_service_consultation("next", args) is True
        assert expected in v._speak_consultation_gate.await_args.kwargs["spoken"].lower()

    caller_says(v, "No.")
    args = {"service_description": "60 minute Swedish massage", "earliest": True}
    prompts_before = v._speak_consultation_gate.await_count
    assert await v._enforce_service_consultation("schedule", args) is False
    assert v._speak_consultation_gate.await_count == prompts_before
    assert args["requested_services"] == ["60 minute Swedish Massage"]
    assert args["earliest"] is True
    assert v.session.entities["consultation_completed_kinds"] == ["massage"]
    assert v.session.entities["consultation_states"]["massage"][
        "selected_duration_minutes"
    ] == 60
    assert not v.session.entities.get("consultation_addon_pending")
    assert v.session.booking_status not in {"booked", "rescheduled"}

    # A later tool call to check Wednesday at 2:30 cannot restart the
    # consultation or lose the selected service or duration.
    caller_says(v, "No, finish the booking. Wednesday at 2:30 PM.")
    args = {
        "service_description": "Swedish Massage",
        "requested_start_iso": "2026-10-14T14:30:00",
        "operation": "schedule",
    }
    assert await v._enforce_service_consultation("availability", args) is False
    assert args["requested_services"] == ["60 minute Swedish Massage"]
    assert args["requested_start_iso"] == "2026-10-14T14:30:00"
    assert v._speak_consultation_gate.await_count == prompts_before


@pytest.mark.asyncio
async def test_no_duration_still_requires_one_real_menu_choice():
    v = voice()
    v._consultation_catalog = AsyncMock(return_value=(DUPLICATE_NAME_MENU, []))
    caller_says(v, "I would like a Swedish Massage.")
    state = _complete_massage()
    v.session.entities.update({
        "consultation_state": state,
        "consultation_states": {"massage": state},
        "consultation_booking_request": {"requested_services": ["Swedish Massage"]},
    })
    args = {"service_description": "Swedish Massage"}
    assert await v._enforce_service_consultation("duration", args) is True
    assert v._speak_consultation_gate.await_args.kwargs["status"] == "duration_options"
    assert "60-minute" in v._speak_consultation_gate.await_args.kwargs["spoken"]
    assert "90-minute" in v._speak_consultation_gate.await_args.kwargs["spoken"]
    caller_says(v, "60 minutes.")
    args = {"service_description": "Swedish Massage"}
    assert await v._enforce_service_consultation("selected", args) is False
    assert args["requested_services"] == ["60 minute Swedish Massage"]


@pytest.mark.asyncio
async def test_other_family_does_not_inherit_duration_before_name_change():
    v = voice()
    caller_says(v, "Let's book a Swedish massage.")
    caller_says(v, "Let's do 90 minutes.")
    caller_says(v, "Actually I'd prefer a Sports Massage.")
    assert v._caller_grounded_consultation_duration(
        "massage", "Sports Massage", DUPLICATE_NAME_MENU
    ) is None


def test_volunteered_back_and_leg_areas_are_not_asked_twice():
    state = initialize_consultation(
        "60 Minute Swedish Massage", services=DUPLICATE_NAME_MENU
    )
    assert state is not None
    first = take_next_question(state)
    assert first["awaiting_field"] == "massage_reason"
    answered = record_pending_answer(
        first["state"], caller_utterance="My back and legs are sore."
    )
    assert answered["status"] == "answer_recorded"
    assert {"massage_reason", "massage_areas"}.issubset(
        set(answered["state"]["answered_fields"])
    )
    next_question = take_next_question(answered["state"])
    assert next_question["awaiting_field"] == "pressure_preference"
    # No raw caller health details should be copied into state.
    assert "my back and legs are sore" not in str(answered["state"]).lower()



@pytest.mark.parametrize("requested_service", [
    "Swedish massage",
    "Massage - Swedish 60 Minutes",
    "60-minute Swedish massage",
])
def test_remembers_early_duration_with_square_category_first_names(requested_service):
    """The October 12 Square catalog import displays the category first."""
    v = voice()
    caller_says(v, "I'd like a Swedish massage.")
    caller_says(v, "Let's do 60 minutes.")
    caller_says(v, "My back and legs are sore.")
    assert v._caller_grounded_consultation_duration(
        "massage", requested_service, SQUARE_CATEGORY_FIRST_MENU
    ) == 60


def test_recovered_duration_selects_actual_square_variation_not_couples_or_mat():
    state = _complete_massage("Swedish Massage", 60)
    result = take_duration_offer(
        state, SQUARE_CATEGORY_FIRST_MENU,
        requested_duration_grounded=True,
    )
    assert result["status"] == "selection_preserved"
    assert result["state"]["selected_service_name"] == "Massage - Swedish 60 Minutes"
    assert result["state"]["selected_duration_minutes"] == 60
    addons = take_massage_addon_offer(
        result["state"], SQUARE_CATEGORY_FIRST_MENU, []
    )
    assert addons["status"] == "no_relevant_addons"
    assert availability_gate(addons["state"])["allow"] is True


@pytest.mark.asyncio
async def test_category_first_square_menu_reaches_booking_handoff_without_reask():
    v = voice()
    v._consultation_catalog = AsyncMock(return_value=(SQUARE_CATEGORY_FIRST_MENU, []))
    caller_says(v, "I'd like a Swedish massage.")
    caller_says(v, "Let's do the 60-minute massage.")
    args = {
        "service_description": "60 minute Swedish massage",
        "requested_start_iso": "2026-10-14T14:30:00",
    }
    assert await v._enforce_service_consultation("reason", args) is True
    caller_says(v, "My back and legs are sore.")
    assert await v._enforce_service_consultation("pressure", {
        "service_description": "Swedish massage",
    }) is True
    assert v._speak_consultation_gate.await_args.kwargs["status"] == "consultation_required"
    assert "pressure" in v._speak_consultation_gate.await_args.kwargs["spoken"].lower()
    caller_says(v, "Medium pressure.")
    assert await v._enforce_service_consultation("safety", {
        "service_description": "Swedish massage",
    }) is True
    caller_says(v, "No injuries.")
    final_args = {"service_description": "Swedish massage"}
    count = v._speak_consultation_gate.await_count
    assert await v._enforce_service_consultation("availability", final_args) is False
    assert v._speak_consultation_gate.await_count == count
    assert final_args["requested_services"] == ["Massage - Swedish 60 Minutes"]
    assert final_args["requested_start_iso"] == "2026-10-14T14:30:00"
    assert v.session.booking_status not in {"booked", "rescheduled"}



def test_square_category_first_alias_does_not_reset_completed_massage_questions():
    state = _complete_massage("Swedish Massage", 60)
    state = take_duration_offer(
        state, SQUARE_CATEGORY_FIRST_MENU, requested_duration_grounded=True
    )["state"]
    state = take_massage_addon_offer(state, SQUARE_CATEGORY_FIRST_MENU, [])["state"]
    assert availability_gate(state)["allow"] is True
    from app.services.consultation_gate import initialize_consultation

    updated = initialize_consultation(
        "Massage - Swedish 60 Minutes",
        services=SQUARE_CATEGORY_FIRST_MENU,
        prior_state=state,
    )
    assert updated is not None
    assert updated["duration_offer_presented"] is True
    assert updated["addon_offer_presented"] is True
    assert updated["selected_duration_minutes"] == 60
    assert len(updated["answered_fields"]) == 4
    assert availability_gate(updated)["allow"] is True


def test_changing_from_swedish_to_sports_does_reset_previous_massage_offer():
    state = _complete_massage("Swedish Massage", 60)
    state = take_duration_offer(
        state, SQUARE_CATEGORY_FIRST_MENU, requested_duration_grounded=True
    )["state"]
    state = take_massage_addon_offer(state, SQUARE_CATEGORY_FIRST_MENU, [])["state"]
    from app.services.consultation_gate import initialize_consultation

    updated = initialize_consultation(
        "Massage - Sports 60 Minutes",
        services=SQUARE_CATEGORY_FIRST_MENU + [
            {"name": "Massage - Sports 60 Minutes", "duration_minutes": 60}
        ],
        prior_state=state,
    )
    assert updated is not None
    assert updated["duration_offer_presented"] is False
    assert updated["addon_offer_presented"] is False
