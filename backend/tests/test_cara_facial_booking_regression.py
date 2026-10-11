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
