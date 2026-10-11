"""Square's category-first menu labels must work throughout voice booking."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.services.catalog_selection import exact_catalog_service, family_variations
from app.services.booking_adapters import SpaBookingAdapter, BookingContext
from app.services.consultation_gate import initialize_consultation, record_consultation_answer, take_duration_offer
from tests.conftest import make_spa
from tests.test_realtime_consultation_enforcement import voice, caller_says

MENU = [
    {"name": "Massage - Swedish 60 Minutes", "duration_minutes": 60,
     "square_variation_id": "swedish-60", "square_variation_version": 7},
    {"name": "Massage - Swedish 90 Minutes", "duration_minutes": 90,
     "square_variation_id": "swedish-90"},
    {"name": "Massage - Swedish with Healthy Line Mat 60 Minutes", "duration_minutes": 60,
     "square_variation_id": "swedish-mat-60"},
    {"name": "Massage - Couples Swedish 60 Minutes", "duration_minutes": 60,
     "square_variation_id": "couples-60"},
]


@pytest.mark.parametrize("service_request", [
    "60 minute Swedish massage", "60-minute Swedish massage",
    "Swedish massage 60 min", "sixty minute Swedish massage",
])
def test_category_first_menu_resolves_exact_requested_variation(service_request):
    assert exact_catalog_service(MENU, service_request) is MENU[0]


def test_family_options_keep_durations_and_modifiers_separate():
    assert family_variations(MENU, "Swedish massage") == MENU[:2]
    assert exact_catalog_service(MENU, "Swedish massage") is None
    assert exact_catalog_service(MENU, "30 minute Swedish massage") is None
    assert exact_catalog_service(MENU[2:], "60 minute Swedish massage") is None
    assert exact_catalog_service(MENU + [dict(MENU[0])], "60 minute Swedish massage") is None


@pytest.mark.asyncio
async def test_voice_consultation_passes_selected_square_id_to_booking():
    request = "60-minute Swedish massage"
    state = initialize_consultation(request, services=MENU)
    for field, answer in [("massage_reason", "relaxation"), ("massage_areas", "all over"),
                          ("pressure_preference", "medium"), ("safety_answered", "no")]:
        state = record_consultation_answer(state, field=field, answer=answer)
    decision = take_duration_offer(state, MENU)
    assert decision["status"] == "selection_preserved"
    state = decision["state"]
    assert state["selected_service_name"] == MENU[0]["name"]
    state["addon_offer_presented"] = True
    v = voice()
    v._consultation_catalog_cache = (MENU, [])
    v._consultation_catalog = AsyncMock(return_value=(MENU, []))
    v.session.entities.update({"consultation_states": {"massage": state},
                               "consultation_state": state})
    caller_says(v, "Book the 60-minute Swedish massage Wednesday at 2:30.")
    args = {"service_description": request, "requested_start_iso": "2026-10-14T14:30:00"}
    assert await v._enforce_service_consultation("swedish", args) is False
    assert args["requested_services"] == [MENU[0]["name"]]
    assert args["requested_start_iso"] == "2026-10-14T14:30:00"
    start = datetime(2026, 10, 14, 14, 30, tzinfo=timezone.utc)
    adapter = SpaBookingAdapter(make_spa(services=MENU))
    ctx, name = adapter._canonicalize_ctx(BookingContext(
        start=start, end=start + timedelta(minutes=60), title=args["service_description"],
        service_description=args["service_description"], customer_phone="+15550000002",
    ))
    assert name == MENU[0]["name"]
    assert ctx.service_variation_id == "swedish-60"
    assert ctx.service_variation_version == 7
    assert ctx.end - ctx.start == timedelta(minutes=60)
