"""Phase 1 regression: HydroLux5 Facial service/variation resolution.

Square models this as one parent item ("HydroLux5 Facial") with several
bookable variations (Face Only / Face + Neck / Face + Neck + Chest). This
tenant's own service menu flattens each variation into its own named entry
("HydroLux5 Facial - Face Only", etc.), each carrying an explicit
`square_variation_id`/`square_variation_version` so `SpaBookingAdapter`
resolves the caller's phrase without any Square catalog text-matching (see
`spa_router.SpaBookingAdapter._apply_entry`/`_canonicalize_ctx`).

These tests exercise `resolve_service` directly — the same function
`check_availability` and `create_booking` both route through — so a caller
saying just "HydroLux5" with several variations configured must be reported
as ambiguous (asked to clarify), never resolved to one arbitrarily.
"""
from datetime import datetime, timedelta, timezone

from app.models import BookingProvider
from app.services.booking_adapters import BookingContext, SpaBookingAdapter
from tests.conftest import make_spa

_HYDROLUX5_SERVICES = [
    {
        "name": "HydroLux5 Facial - Face Only",
        "duration_minutes": 45,
        "square_variation_id": "var_face_only",
        "square_variation_version": 3,
    },
    {
        "name": "HydroLux5 Facial - Face + Neck",
        "duration_minutes": 60,
        "square_variation_id": "var_face_neck",
        "square_variation_version": 5,
    },
    {
        "name": "HydroLux5 Facial - Face + Neck + Chest",
        "duration_minutes": 75,
        "square_variation_id": "var_face_neck_chest",
        "square_variation_version": 2,
    },
]


def _adapter():
    spa = make_spa(services=_HYDROLUX5_SERVICES)
    return SpaBookingAdapter(spa)


def test_hydrolux5_face_only_resolves_to_its_configured_variation():
    resolution = _adapter().resolve_service("HydroLux5 Facial - Face Only")
    assert resolution.status == "resolved"
    assert resolution.entry["square_variation_id"] == "var_face_only"
    assert resolution.entry["square_variation_version"] == 3


def test_hydrolux5_face_and_neck_resolves_to_its_configured_variation():
    resolution = _adapter().resolve_service("HydroLux5 Facial - Face + Neck")
    assert resolution.status == "resolved"
    assert resolution.entry["square_variation_id"] == "var_face_neck"
    assert resolution.entry["square_variation_version"] == 5


def test_hydrolux5_face_neck_and_chest_resolves_to_its_configured_variation():
    resolution = _adapter().resolve_service("HydroLux5 Facial - Face + Neck + Chest")
    assert resolution.status == "resolved"
    assert resolution.entry["square_variation_id"] == "var_face_neck_chest"
    assert resolution.entry["square_variation_version"] == 2


def test_bare_hydrolux5_with_multiple_variations_asks_for_clarification():
    """The caller naming only "HydroLux5" with three configured variations
    must never be silently resolved to one of them."""
    resolution = _adapter().resolve_service("HydroLux5")
    assert resolution.status == "ambiguous"
    assert len(resolution.options) == 3


async def test_check_availability_surfaces_the_same_clarification_not_a_silent_pick(monkeypatch):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        services=_HYDROLUX5_SERVICES,
    )
    adapter = SpaBookingAdapter(spa)

    async def _reject(*_a, **_k):
        raise AssertionError("Square must not be reached while the service is ambiguous")

    monkeypatch.setattr(adapter.delegate, "_request", _reject)

    start = datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc)
    ctx = BookingContext(
        start=start,
        end=start + timedelta(hours=1),
        title="HydroLux5",
        customer_phone="+15550001",
        service_description="HydroLux5",
    )

    verdict = await adapter.check_availability(ctx)
    assert not verdict.available
    assert "service_ambiguous" in verdict.reason
