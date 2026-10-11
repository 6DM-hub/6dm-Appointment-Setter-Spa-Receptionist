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
    record_duration_selection,
    take_duration_offer,
    take_massage_addon_offer,
)
from tests.test_realtime_consultation_enforcement import caller_says, voice


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
    # duplicate-named variants. Offer menu-grounded durations instead.
    assert res["status"] == "selection_preserved" or res["status"] == "offer_duration"
    assert res["state"]["selected_duration_minutes"] == 60


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
        ("My back and legs are sore.", "particular areas"),
        ("My back and my legs, like I said.", "pressure"),
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
