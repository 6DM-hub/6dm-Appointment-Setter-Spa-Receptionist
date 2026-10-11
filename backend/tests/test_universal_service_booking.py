"""Universal, tenant-menu-backed booking regressions.

No live Square, Twilio or other paid service calls occur in these tests.
The calendar/appointment provider remains the authority for actual availability.
"""
from unittest.mock import AsyncMock

import pytest

from app.services.catalog_selection import (
    configured_duration, duration_from_name, exact_catalog_service,
    family_variations,
)
from app.services.consultation_gate import (
    initialize_consultation, record_consultation_answer, take_duration_offer,
)
from app.services.service_consultation import configured_duration_choices
from tests.test_realtime_consultation_enforcement import caller_says, voice


SERVICES = [
    {"name": "60 Minute Swedish Massage", "duration_minutes": 60,
     "service_family": "Swedish Massage", "consultation_kind": "massage"},
    {"name": "90 Minute Swedish Massage", "duration_minutes": 90,
     "service_family": "Swedish Massage", "consultation_kind": "massage"},
    {"name": "60 Minute Sports Massage", "duration_minutes": 60,
     "service_family": "Sports Massage", "consultation_kind": "massage"},
    {"name": "30 Minute European Facial", "duration_minutes": 30,
     "service_family": "European Facial", "consultation_kind": "facial"},
    {"name": "75 Minute European Facial", "duration_minutes": 75,
     "service_family": "European Facial", "consultation_kind": "facial"},
    {"name": "Eyebrow Wax", "duration_minutes": 15,
     "aliases": ["Brow Wax"], "category": "Waxing"},
    {"name": "Lash Lift", "duration_minutes": 45, "category": "Lash"},
    {"name": "Nail Treatment", "duration_minutes": 50, "category": "Nails"},
    {"name": "Hot Stones", "duration_minutes": 15, "is_add_on": True},
]


@pytest.mark.parametrize(("utterance", "minutes"), [
    ("90-minute Swedish massage", 90),
    ("75 minute European facial", 75),
    ("45 min lash lift", 45),
    ("60 minute Swedish Massage", 60),
    ("2:30 PM", None),
    ("Swedish Massage", None),
])
def test_duration_extracted_from_named_service_not_clock_time(utterance, minutes):
    assert duration_from_name(utterance) == minutes


@pytest.mark.parametrize(("request", "expected"), [
    ("90-minute Swedish Massage", "90 Minute Swedish Massage"),
    ("75 minute European Facial", "75 Minute European Facial"),
    ("Brow Wax", "Eyebrow Wax"),
    ("Eyebrow Wax", "Eyebrow Wax"),
    ("Lash Lift", "Lash Lift"),
    ("Nail Treatment", "Nail Treatment"),
])
def test_exact_real_menu_service_for_any_modality(request, expected):
    service = exact_catalog_service(SERVICES, request)
    assert service is not None
    assert service["name"] == expected


@pytest.mark.parametrize("service", ["Brow Wax", "Lash Lift", "Nail Treatment"])
def test_non_consultative_menu_services_proceed_without_massage_questions(service):
    v = voice()
    args = {"requested_services": [service], "requested_start_iso": "2026-10-14T14:30:00"}
    v._canonicalize_menu_services(args, SERVICES)
    assert len(args["requested_services"]) == 1
    assert args["requested_services"][0] in {"Eyebrow Wax", "Lash Lift", "Nail Treatment"}


def test_no_cross_family_or_invented_duration_substitution():
    variants = family_variations(SERVICES, "Swedish Massage")
    assert [item["name"] for item in variants] == [
        "60 Minute Swedish Massage",
        "90 Minute Swedish Massage",
    ]
    assert exact_catalog_service(SERVICES, "30-minute Swedish Massage") is None
    assert exact_catalog_service(SERVICES, "Swedish Massage") is None
    assert exact_catalog_service(SERVICES, "90 Minute Sports Massage") is None
    assert not family_variations(SERVICES, "massage")
    assert all("Sports" not in item["name"] for item in variants)
    assert [choice["minutes"] for choice in
            configured_duration_choices(SERVICES, "Swedish Massage")["choices"]] == [60, 90]


def test_aliases_cannot_disambiguate_multiple_different_catalog_variations():
    candidates = [
        {"name": "60 Minute Deep Tissue Massage", "duration_minutes": 60,
         "aliases": ["deep tissue"]},
        {"name": "90 Minute Deep Tissue Massage", "duration_minutes": 90,
         "aliases": ["deep tissue"]},
    ]
    assert exact_catalog_service(candidates, "deep tissue") is None
    assert exact_catalog_service(candidates, "90-minute deep tissue") is None


def _answered_massage(name):
    state = initialize_consultation(name, services=SERVICES)
    assert state is not None
    for field, value in (
        ("massage_reason", "relaxation"),
        ("massage_areas", "all over"),
        ("pressure_preference", "medium"),
        ("safety_answered", "no"),
    ):
        state = record_consultation_answer(state, field=field, answer=value)
    return state


def test_explicit_90_minute_massage_preserved_with_no_duration_reask():
    state = _answered_massage("90-minute Swedish Massage")
    decision = take_duration_offer(state, SERVICES)
    assert decision["status"] == "selection_preserved"
    assert decision["offer"] is None
    assert decision["state"]["selected_service_name"] == "90 Minute Swedish Massage"
    assert decision["state"]["selected_duration_minutes"] == 90


def test_ambiguous_swedish_massage_only_offers_60_and_90_from_same_family():
    state = _answered_massage("Swedish Massage")
    decision = take_duration_offer(state, SERVICES)
    assert decision["status"] == "offer_duration"
    assert [item["minutes"] for item in decision["state"]["duration_choices"]] == [60, 90]
    assert "30-minute" not in decision["offer"]["text"]
    assert "90-minute" in decision["offer"]["text"]


def test_90_minute_name_does_not_map_to_60_minute_even_when_60_exists():
    assert configured_duration(exact_catalog_service(SERVICES, "90 Minute Swedish Massage")) == 90
    assert exact_catalog_service(SERVICES, "45 Minute Swedish Massage") is None


def test_voice_duration_choices_are_menu_driven():
    v = voice()
    state = _answered_massage("Swedish Massage")
    state = take_duration_offer(state, SERVICES)["state"]
    for utterance, expected in (
        ("90 minutes", 90),
        ("ninety minutes", 90),
        ("longer", 90),
        ("60 minutes", 60),
        ("shorter", 60),
        ("2:30 PM", None),
        ("Swedish massage", None),
    ):
        assert v._consultation_choice_from_caller(state, utterance) == expected


def test_multi_service_canonicalization_preserves_order_date_and_time():
    v = voice()
    args = {
        "requested_services": ["90 minute Swedish massage", "Brow Wax"],
        "requested_start_iso": "2026-10-14T14:30:00",
        "caller_name": "Jane Customer",
    }
    v._canonicalize_menu_services(args, SERVICES)
    assert args["requested_services"] == ["90 Minute Swedish Massage", "Eyebrow Wax"]
    assert args["service_description"] == "90 Minute Swedish Massage + Eyebrow Wax"
    assert args["requested_start_iso"] == "2026-10-14T14:30:00"
    assert args["caller_name"] == "Jane Customer"


@pytest.mark.asyncio
async def test_optional_massage_addon_unclear_answer_cannot_repeat_forever():
    v = voice()
    v._consultation_catalog = AsyncMock(return_value=(SERVICES, []))
    state = _answered_massage("90 Minute Swedish Massage")
    state = take_duration_offer(state, SERVICES)["state"]
    # Mimic a previously spoken, provider-approved add-on offer.
    state["addon_offer_presented"] = True
    v.session.entities.update({
        "consultation_state": state,
        "consultation_states": {"massage": state},
        "consultation_addon_pending": {
            "massage": {"turn": 0, "names": ["Hot Stones"]},
        },
        "consultation_booking_request": {
            "requested_services": ["90 Minute Swedish Massage"],
            "requested_start_iso": "2026-10-14T14:30:00",
        },
    })
    caller_says(v, "Maybe.")
    assert await v._enforce_service_consultation(
        "addon-clarification", {"requested_services": ["90 Minute Swedish Massage"]}
    ) is True
    assert v._speak_consultation_gate.await_args.kwargs["status"] == "addon_selection_required"
    caller_says(v, "Not sure.")
    args = {"requested_services": ["90 Minute Swedish Massage"]}
    assert await v._enforce_service_consultation("addon-original", args) is False
    assert args["requested_services"] == ["90 Minute Swedish Massage"]
    assert args["requested_start_iso"] == "2026-10-14T14:30:00"
    assert v.session.entities["consultation_addon_pending"] == {}
    assert v.session.booking_status not in {"booked", "rescheduled"}


@pytest.mark.asyncio
async def test_generic_massage_unmatched_category_requires_actual_menu_choice():
    v = voice()
    v._consultation_catalog = AsyncMock(return_value=(SERVICES, []))
    state = _answered_massage("massage")
    v.session.entities["consultation_state"] = state
    v.session.entities["consultation_states"] = {"massage": state}
    caller_says(v, "I'd like a massage on Wednesday at 2:30 PM.")
    args = {"requested_services": ["massage"], "requested_start_iso": "2026-10-14T14:30:00"}
    assert await v._enforce_service_consultation("choice", args) is True
    assert v._speak_consultation_gate.await_args.kwargs["status"] == "menu_service_required"
    assert "swedish massage" in v._speak_consultation_gate.await_args.kwargs["spoken"].lower()
    assert "sports massage" in v._speak_consultation_gate.await_args.kwargs["spoken"].lower()

    caller_says(v, "Swedish massage.")
    args = {"requested_services": ["massage"]}
    assert await v._enforce_service_consultation("family", args) is True
    assert v._speak_consultation_gate.await_args.kwargs["status"] == "duration_options"
    assert "90-minute" in v._speak_consultation_gate.await_args.kwargs["spoken"]

    caller_says(v, "90 minutes please.")
    args = {"requested_services": ["massage"]}
    assert await v._enforce_service_consultation("duration", args) is False
    assert args["requested_services"] == ["90 Minute Swedish Massage"]
    assert args["requested_start_iso"] == "2026-10-14T14:30:00"
