"""Regression coverage for the Texas/Square booking bugs.

Reported symptoms, root-caused and fixed here:

  * "2pm Thursday" was rejected as outside business hours, and a caller-picked
    Saturday 10am slot was accepted then rejected — both traced to
    `requested_start_iso` being labelled UTC ("...T14:00:00Z") when it was
    actually the caller's LOCAL time. See `_parse_dt` in
    `appointment_booking_service.py`, and the tool-schema / prompt wording in
    `xai_realtime.py` / `grok_service.py` that used to suggest a "Z" suffix.
  * "European Facial" -> `service_not_recognized` even though Square's catalog
    search returned 200: the catalog matcher required byte-exact equality
    between the caller's phrase and Square's item/variation name, which fails
    whenever a catalog splits "European Facial" (item) from "60 Min"
    (variation). See `SquareAdapter._resolve_service_variation`.
  * No dynamic therapist handling: `preferred_staff` is threaded from the
    caller's words through `AppointmentIntent` / `BookingContext` into a new
    Square team-member resolution that never invents a substitute.
"""
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import BookingProvider
from app.services import appointment_booking_service
from app.services.appointment_booking_service import (
    BookingOutcome,
    _parse_dt,
    check_availability_only,
    confirm_booking,
    stage_booking,
)
from app.services.booking_adapters import BookingContext, SpaBookingAdapter
from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_adapters.providers.square import (
    ServiceResolutionError,
    TeamMemberResolutionError,
)
from app.services.business_hours import resolve_timezone
from app.services.call_state import CallSession
from app.services.grok_service import (
    AppointmentIntent,
    build_realtime_instructions,
)
from app.services.xai_realtime import (
    CHECK_AVAILABILITY_TOOL,
    CONFIRM_APPOINTMENT_TOOL,
    MANAGE_APPOINTMENT_TOOL,
    PROPOSE_APPOINTMENT_TOOL,
)
from tests.conftest import make_spa


async def _async_value(value):
    return value


class _FakeSquareTransport:
    """Routes Square `_request` calls by path; records every call made."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.catalog_items: list[dict] = []
        self.availabilities: list[dict] = []
        self.team_members: list[dict] = []
        self.customers: list[dict] = []
        self.created_customer: dict = {"id": "cust_1"}
        self.created_booking: dict = {"id": "sq_book_1", "status": "ACCEPTED"}

    async def __call__(self, method: str, path: str, *, json: dict | None = None) -> dict:
        self.calls.append((method, path, json or {}))
        if path.startswith("/v2/locations/"):
            loc_id = path.rsplit("/", 1)[-1]
            return {
                "location": {
                    "id": loc_id,
                    "name": "Test Square Location",
                    "timezone": "America/Chicago",
                    "status": "ACTIVE",
                }
            }
        if path == "/v2/catalog/search-catalog-items":
            return {"items": self.catalog_items}
        if path == "/v2/bookings/availability/search":
            return {"availabilities": self.availabilities}
        if path == "/v2/team-members/search":
            return {"team_members": self.team_members}
        if path == "/v2/customers/search":
            return {"customers": self.customers}
        if path == "/v2/customers":
            return {"customer": self.created_customer}
        if path == "/v2/bookings":
            return {"booking": self.created_booking}
        raise AssertionError(f"unexpected Square path called: {path}")

    def calls_to(self, path: str) -> list[dict]:
        return [payload for _, p, payload in self.calls if p == path]


def _square_adapter(spa) -> "SquareAdapter":  # noqa: F821 - runtime type
    return SpaBookingAdapter(spa).delegate


def _ctx(**overrides) -> BookingContext:
    start = overrides.pop("start", datetime(2026, 9, 24, 19, 0, tzinfo=timezone.utc))
    end = overrides.pop("end", start + timedelta(hours=1))
    defaults = dict(
        start=start,
        end=end,
        title="Deep tissue massage",
        customer_phone="+15550001",
        service_description="Deep tissue massage",
    )
    defaults.update(overrides)
    return BookingContext(**defaults)


# --------------------------------------------------------------------------- #
# 1. Timezone / DST — the core reported bug
# --------------------------------------------------------------------------- #


def test_naive_caller_datetime_is_localized_to_the_tenant_timezone_not_utc():
    """A bare '2026-09-24T14:00:00' (no offset) is the caller's LOCAL 2pm at a
    Chicago spa. Treating it as UTC (the old `.replace(tzinfo=timezone.utc)`
    behaviour) reads back as 9am Chicago in September (CDT, UTC-5) — before a
    10am opening. Localizing to the tenant tz keeps it as 2pm Chicago."""
    tz = resolve_timezone("America/Chicago")
    parsed = _parse_dt("2026-09-24T14:00:00", tz)

    assert parsed is not None
    local = parsed.astimezone(tz)
    assert (local.hour, local.minute) == (14, 0)
    # And in UTC terms, September is CDT (UTC-5): 14:00 local -> 19:00 UTC.
    assert parsed.astimezone(timezone.utc).hour == 19


def test_naive_caller_datetime_respects_dst_across_seasons():
    """Same wall-clock '2pm', two different seasons: CDT (UTC-5) in September,
    CST (UTC-6) in January. No hardcoded offset should be involved — ZoneInfo
    must resolve each one correctly on its own."""
    tz = resolve_timezone("America/Chicago")

    summer = _parse_dt("2026-09-24T14:00:00", tz)
    winter = _parse_dt("2026-01-08T14:00:00", tz)

    assert summer.astimezone(timezone.utc).hour == 19  # CDT, UTC-5
    assert winter.astimezone(timezone.utc).hour == 20  # CST, UTC-6


def test_explicit_utc_offset_is_trusted_and_not_relocalized():
    """A value that already carries a real offset (not a naive string) is not
    touched — only genuinely naive values get localized."""
    tz = resolve_timezone("America/Chicago")
    parsed = _parse_dt("2026-09-24T19:00:00+00:00", tz)

    assert parsed.utcoffset() == timedelta(0)
    assert parsed.astimezone(timezone.utc).hour == 19


@pytest.mark.asyncio
async def test_thursday_2pm_local_is_inside_business_hours_end_to_end(monkeypatch):
    """Reproduces the reported call: "60-minute deep tissue Thursday at 2pm"
    at a Texas spa (America/Chicago, 10am-6pm Thursdays) must be accepted, not
    rejected as outside hours."""
    spa = make_spa(
        business_hours={"thu": [{"open": "10:00", "close": "18:00"}]},
        timezone="America/Chicago",
        services=[{"name": "Deep tissue massage", "duration_minutes": 60}],
    )
    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(spa))
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))

    session = CallSession(
        "call-thu-2pm", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id)
    )
    intent = AppointmentIntent(
        intent="inquiry",
        confidence=1.0,
        # Local wall-clock, exactly what the fixed prompt now instructs the
        # model to emit — no "Z", no offset.
        requested_start_iso="2026-09-24T14:00:00",
        service_description="60-minute deep tissue massage",
    )

    result = await check_availability_only(None, session, intent)

    assert result.available, result.message


@pytest.mark.asyncio
async def test_thursday_2pm_ends_at_3pm_not_pushed_past_closing(monkeypatch):
    """The 60-minute appointment must end at 3pm local, comfortably inside a
    10am-6pm day — verifies duration is honoured, not silently lengthened."""
    spa = make_spa(
        business_hours={"thu": [{"open": "10:00", "close": "18:00"}]},
        timezone="America/Chicago",
        services=[{"name": "Deep tissue massage", "duration_minutes": 60}],
    )
    tz = resolve_timezone(spa.timezone)
    start = _parse_dt("2026-09-24T14:00:00", tz)
    end = start + timedelta(minutes=60)

    assert end.astimezone(tz).strftime("%H:%M") == "15:00"
    assert appointment_booking_service._duration_minutes(
        SpaBookingAdapter(spa), "60-minute deep tissue massage"
    ) == 60


@pytest.mark.asyncio
async def test_saturday_10am_local_is_inside_business_hours(monkeypatch):
    """The second reported failure: Square/agent offers Saturday 10am, 11:30am,
    2pm; caller picks 10am; it must not then be rejected as closed. Saturday
    hours are 10am-4pm."""
    spa = make_spa(
        business_hours={"sat": [{"open": "10:00", "close": "16:00"}]},
        timezone="America/Chicago",
        services=[{"name": "Deep tissue massage", "duration_minutes": 60}],
    )
    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(spa))
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))

    session = CallSession(
        "call-sat-10am", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id)
    )
    intent = AppointmentIntent(
        intent="inquiry",
        confidence=1.0,
        requested_start_iso="2026-09-26T10:00:00",
        service_description="Deep tissue massage",
    )

    result = await check_availability_only(None, session, intent)

    assert result.available, result.message


# --------------------------------------------------------------------------- #
# 2. Prompt-contract regression guards
# --------------------------------------------------------------------------- #


def test_realtime_tool_schemas_never_suggest_a_utc_z_suffix():
    """Regression guard: the tool schemas used to literally instruct the model
    to format local time as '...T14:00:00Z', which is what caused the model to
    mislabel local time as UTC in the first place."""
    for tool in (PROPOSE_APPOINTMENT_TOOL, CONFIRM_APPOINTMENT_TOOL, MANAGE_APPOINTMENT_TOOL, CHECK_AVAILABILITY_TOOL):
        for prop_name, schema in tool["parameters"].get("properties", {}).items():
            description = str(schema.get("description", ""))
            assert "T14:00:00Z" not in description, (tool["name"], prop_name)
            assert not description.rstrip().endswith("Z."), (tool["name"], prop_name)


def test_propose_and_check_availability_tools_explicitly_forbid_utc():
    for tool in (PROPOSE_APPOINTMENT_TOOL, MANAGE_APPOINTMENT_TOOL, CHECK_AVAILABILITY_TOOL):
        description = str(tool["parameters"]["properties"]["requested_start_iso"]["description"])
        assert "LOCAL" in description
        assert "no offset" in description.lower() or "no timezone" in description.lower() or "no 'z'" in description.lower()


def test_propose_appointment_tool_carries_preferred_staff():
    props = PROPOSE_APPOINTMENT_TOOL["parameters"]["properties"]
    assert "preferred_staff" in props


def test_build_realtime_instructions_names_the_tenant_timezone_and_local_time():
    instructions = build_realtime_instructions("Your Day Spa", None, "America/Chicago")

    assert "America/Chicago" in instructions
    assert "UTC" in instructions  # still given, but only "for your reference"
    assert "never" in instructions.lower()
    assert "closing-time cutoff" in instructions.lower() or "last bookable start time" in instructions.lower()


def test_build_realtime_instructions_falls_back_to_utc_for_unknown_tenant():
    """No timezone on the session (e.g. outbound sales) must not crash."""
    instructions = build_realtime_instructions("6DM", None, None)
    assert instructions  # renders without raising


# --------------------------------------------------------------------------- #
# 3. Service resolution — "European Facial" style catalog mismatches
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_square_catalog_matches_item_name_even_when_variation_name_is_just_the_duration(monkeypatch):
    """Reproduces `needs_clarification: service_not_recognized: European
    Facial` against a live 200 catalog response: Square's item is named
    "European Facial" and its variation is named "60 Min" — no exact string
    equals the caller's phrase, but the item-name subset match now does."""
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = _square_adapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {
            "item_data": {
                "name": "European Facial",
                "variations": [
                    {
                        "id": "var_facial_60",
                        "version": 3,
                        "item_variation_data": {"name": "60 Min"},
                    }
                ],
            }
        }
    ]
    monkeypatch.setattr(adapter, "_request", transport)

    resolved = await adapter._resolve_service_variation(
        _ctx(title="European Facial", service_description="European Facial")
    )

    assert resolved["id"] == "var_facial_60"


@pytest.mark.asyncio
async def test_square_ambiguous_catalog_variations_still_ask_for_clarification(monkeypatch):
    """A subset match must not silently pick a duration when the item has more
    than one bookable variation and the caller didn't specify which."""
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = _square_adapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {
            "item_data": {
                "name": "European Facial",
                "variations": [
                    {"id": "var_facial_30", "version": 1, "item_variation_data": {"name": "30 Min"}},
                    {"id": "var_facial_60", "version": 3, "item_variation_data": {"name": "60 Min"}},
                ],
            }
        }
    ]
    monkeypatch.setattr(adapter, "_request", transport)

    with pytest.raises(ServiceResolutionError) as exc_info:
        await adapter._resolve_service_variation(
            _ctx(title="European Facial", service_description="European Facial")
        )
    assert "ambiguous" in exc_info.value.reason


@pytest.mark.asyncio
async def test_square_uses_explicit_context_variation_id_without_any_catalog_call(monkeypatch):
    """When the tenant's own service-menu entry already carries a
    `square_variation_id`, Square must use it directly — zero text matching,
    zero guessing, zero catalog round-trip."""
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = _square_adapter(spa)

    async def _reject(*_args, **_kwargs):
        raise AssertionError("Square catalog must not be called when ctx already carries an explicit variation id")

    monkeypatch.setattr(adapter, "_request", _reject)

    ctx = _ctx(service_variation_id="var_explicit", service_variation_version=7)
    resolved = await adapter._resolve_service_variation(ctx)

    assert resolved["id"] == "var_explicit"
    assert resolved["version"] == 7


def test_spa_router_attaches_configured_square_variation_id_from_tenant_menu():
    """The tenant's own services JSONB can carry an explicit Square mapping
    per service; `SpaBookingAdapter` must attach it once it resolves the
    caller's phrase to that canonical service."""
    spa = make_spa(
        services=[
            {
                "name": "European Facial",
                "duration_minutes": 60,
                "square_variation_id": "var_explicit",
                "square_variation_version": 5,
            }
        ]
    )
    adapter = SpaBookingAdapter(spa)
    ctx = _ctx(title="facial", service_description="european facial please")

    canonical_ctx, canonical_name = adapter._canonicalize_ctx(ctx)

    assert canonical_name == "European Facial"
    assert canonical_ctx.service_variation_id == "var_explicit"
    assert canonical_ctx.service_variation_version == 5


@pytest.mark.asyncio
async def test_create_booking_canonicalizes_service_the_same_way_check_availability_does(monkeypatch):
    """`create_booking` must resolve the exact same service `check_availability`
    already validated — not re-derive it from the caller's raw words a second
    time, which previously could resolve differently (or fail outright)."""
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        services=[
            {
                "name": "European Facial",
                "duration_minutes": 60,
                "square_variation_id": "var_explicit",
                "square_variation_version": 5,
            }
        ],
    )
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.availabilities = [
        {
            "start_at": "2026-09-24T19:00:00Z",
            "location_id": "loc_123",
            "appointment_segments": [
                {
                    "team_member_id": "TM1",
                    "service_variation_id": "var_explicit",
                    "service_variation_version": 5,
                    "duration_minutes": 60,
                }
            ],
        }
    ]
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    ctx = _ctx(title="facial", service_description="european facial please")
    result = await adapter.create_booking(ctx)

    assert result.external_id == "sq_book_1"
    # Never had to text-match against the live catalog at all.
    assert transport.calls_to("/v2/catalog/search-catalog-items") == []


# --------------------------------------------------------------------------- #
# 4. Therapist / team-member handling
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_staff_preference_omits_team_filter_so_any_qualified_staff_is_returned(monkeypatch):
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = _square_adapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 1, "item_variation_data": {"name": "Regular"}}]}}
    ]
    transport.availabilities = []
    monkeypatch.setattr(adapter, "_request", transport)

    await adapter._find_exact_availability(_ctx(preferred_staff=None))

    search_call = transport.calls_to("/v2/bookings/availability/search")[0]
    segment_filter = search_call["query"]["filter"]["segment_filters"][0]
    assert "team_member_id_filter" not in segment_filter


@pytest.mark.asyncio
async def test_caller_requested_staff_member_is_resolved_and_filtered_to_exactly_them(monkeypatch):
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = _square_adapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 1, "item_variation_data": {"name": "Regular"}}]}}
    ]
    transport.team_members = [
        {"id": "TM_SARAH", "given_name": "Sarah", "family_name": "Lee"},
        {"id": "TM_BOB", "given_name": "Bob", "family_name": "Ray"},
    ]
    transport.availabilities = []
    monkeypatch.setattr(adapter, "_request", transport)

    await adapter._find_exact_availability(_ctx(preferred_staff="Sarah"))

    search_call = transport.calls_to("/v2/bookings/availability/search")[0]
    segment_filter = search_call["query"]["filter"]["segment_filters"][0]
    assert segment_filter["team_member_id_filter"] == {"any": ["TM_SARAH"]}


@pytest.mark.asyncio
async def test_unmatched_requested_staff_member_is_refused_not_substituted(monkeypatch):
    """Asking for a therapist who isn't found must not silently fall back to
    booking with somebody else."""
    spa = make_spa(timezone="America/Chicago", business_hours={"thu": [{"open": "09:00", "close": "18:00"}]}, booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 1, "item_variation_data": {"name": "Regular"}}]}}
    ]
    transport.team_members = [{"id": "TM_BOB", "given_name": "Bob", "family_name": "Ray"}]
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    verdict = await adapter.check_availability(_ctx(preferred_staff="Nonexistent Person"))

    assert not verdict.available
    assert "requested_staff_not_available" in verdict.reason
    # Never even reached the availability search once resolution failed.
    assert transport.calls_to("/v2/bookings/availability/search") == []


@pytest.mark.asyncio
async def test_create_booking_raises_instead_of_substituting_when_staff_becomes_unavailable(monkeypatch):
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 1, "item_variation_data": {"name": "Regular"}}]}}
    ]
    transport.team_members = []  # Sarah is gone by the time we recheck
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    with pytest.raises(BookingProviderError):
        await adapter.create_booking(_ctx(preferred_staff="Sarah"))


# --------------------------------------------------------------------------- #
# 5. No hallucinated availability; exact Square slot preserved through booking
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_no_square_availability_is_reported_as_unavailable_not_invented(monkeypatch):
    spa = make_spa(timezone="America/Chicago", business_hours={"thu": [{"open": "09:00", "close": "18:00"}]}, booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 1, "item_variation_data": {"name": "Regular"}}]}}
    ]
    transport.availabilities = []
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    verdict = await adapter.check_availability(_ctx())

    assert not verdict.available
    assert verdict.reason  # a real, provider-sourced reason, not a fabricated time


@pytest.mark.asyncio
async def test_create_booking_sends_the_exact_segment_square_availability_returned(monkeypatch):
    """The booking payload must reuse Square's own returned segment verbatim —
    not something reconstructed from the caller's request."""
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 4, "item_variation_data": {"name": "Regular"}}]}}
    ]
    transport.availabilities = [
        {
            "start_at": "2026-09-24T19:00:00Z",
            "location_id": "loc_123",
            "appointment_segments": [
                {
                    "team_member_id": "TM_SARAH",
                    "service_variation_id": "var1",
                    "service_variation_version": 4,
                    "duration_minutes": 60,
                }
            ],
        }
    ]
    transport.customers = [{"id": "cust_1"}]
    transport.created_booking = {"id": "sq_book_42", "status": "ACCEPTED"}
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    result = await adapter.create_booking(_ctx())

    assert result.external_id == "sq_book_42"
    create_call = transport.calls_to("/v2/bookings")[0]
    segment = create_call["booking"]["appointment_segments"][0]
    assert segment == {
        "team_member_id": "TM_SARAH",
        "service_variation_id": "var1",
        "service_variation_version": 4,
        "duration_minutes": 60,
    }
    assert create_call["booking"]["start_at"] == "2026-09-24T19:00:00Z"


@pytest.mark.asyncio
async def test_create_booking_raises_when_square_rejects_the_booking(monkeypatch):
    """No booking confirmation may occur when Square's create call fails."""
    spa = make_spa(booking_provider=BookingProvider.SQUARE, booking_config={"access_token": "t", "location_id": "loc_123"})
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.catalog_items = [
        {"item_data": {"name": "Deep tissue massage", "variations": [{"id": "var1", "version": 1, "item_variation_data": {"name": "Regular"}}]}}
    ]
    transport.availabilities = [
        {
            "start_at": "2026-09-24T19:00:00Z",
            "location_id": "loc_123",
            "appointment_segments": [
                {"team_member_id": "TM1", "service_variation_id": "var1", "service_variation_version": 1, "duration_minutes": 60}
            ],
        }
    ]
    transport.customers = [{"id": "cust_1"}]

    async def _request(method, path, *, json=None):
        if path == "/v2/bookings":
            raise BookingProviderError("Square API request failed: BAD_REQUEST")
        return await transport(method, path, json=json)

    monkeypatch.setattr(adapter.delegate, "_request", _request)

    with pytest.raises(BookingProviderError):
        await adapter.create_booking(_ctx())


# --------------------------------------------------------------------------- #
# 6. Full booking-engine round trip
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_confirm_booking_end_to_end_via_square_with_local_time_and_no_staff_preference(monkeypatch):
    """The scenario from the bug report, driven through the real booking
    engine: local Thursday 2pm request, Square resolves + books it, and the
    resulting appointment carries the real Square booking id."""
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        business_hours={"thu": [{"open": "10:00", "close": "18:00"}]},
        timezone="America/Chicago",
        services=[
            {
                "name": "Deep tissue massage",
                "duration_minutes": 60,
                "square_variation_id": "var_dt_60",
                "square_variation_version": 2,
            }
        ],
    )
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.availabilities = [
        {
            "start_at": "2026-09-24T19:00:00Z",  # 2pm CDT
            "location_id": "loc_123",
            "appointment_segments": [
                {
                    "team_member_id": "TM_ANY",
                    "service_variation_id": "var_dt_60",
                    "service_variation_version": 2,
                    "duration_minutes": 60,
                }
            ],
        }
    ]
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    monkeypatch.setattr(appointment_booking_service, "_load_spa", lambda *_: _async_value(spa))
    monkeypatch.setattr(appointment_booking_service, "get_booking_adapter", lambda **_: adapter)
    monkeypatch.setattr(
        appointment_booking_service, "_resolve_contact", lambda *_: _async_value(SimpleNamespace(id=uuid.uuid4()))
    )
    monkeypatch.setattr(appointment_booking_service, "_has_conflict", lambda *_a, **_k: _async_value(False))
    monkeypatch.setattr(appointment_booking_service, "_unresolved_booking", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "_load_by_intent_key", lambda *_: _async_value(None))
    monkeypatch.setattr(appointment_booking_service, "_load_call_log_id", lambda *_: _async_value(None))

    class _FakeDB:
        def add(self, _obj):
            return None

        async def flush(self):
            return None

        async def commit(self):
            return None

        async def rollback(self):
            return None

        async def refresh(self, _obj):
            return None

    session = CallSession(
        "call-e2e", "inbound", "+15550000002", "+15550000001", tenant_id=str(spa.id)
    )
    intent = AppointmentIntent(
        intent="schedule",
        confidence=1.0,
        requested_start_iso="2026-09-24T14:00:00",  # local, no offset, as the fixed prompt now requires
        service_description="60-minute deep tissue massage",
    )

    staged = await stage_booking(_FakeDB(), session, intent)
    assert staged.outcome is BookingOutcome.DRAFT
    from app.services.booking_state import arm_verified_proposal, get_draft, save_draft
    named = get_draft(session)
    named.caller_name = "Ada"
    save_draft(session, named)
    arm_verified_proposal(session)
    result = await confirm_booking(_FakeDB(), session)

    assert result.outcome is BookingOutcome.BOOKED, result.message
    assert result.appointment.external_booking_id == "sq_book_1"
    assert result.appointment.booking_provider == "square"
    create_call = transport.calls_to("/v2/bookings")[0]
    assert create_call["booking"]["start_at"] == "2026-09-24T19:00:00Z"


def _friday_slot(hour_utc: int, team: str = "TM_ANY") -> dict:
    return {
        "start_at": f"2026-10-02T{hour_utc:02d}:00:00Z",
        "location_id": "loc_123",
        "appointment_segments": [
            {
                "team_member_id": team,
                "service_variation_id": "var_massage",
                "service_variation_version": 1,
                "duration_minutes": 60,
            }
        ],
    }


@pytest.mark.asyncio
async def test_friday_8am_square_slot_is_rejected_when_dashboard_opens_at_10(monkeypatch):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        timezone="America/Chicago",
        business_hours={"fri": [{"open": "10:00", "close": "18:00"}]},
        services=[{
            "name": "Swedish massage",
            "duration_minutes": 60,
            "square_variation_id": "var_massage",
            "square_variation_version": 1,
        }],
    )
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    # 13:00Z is 8:00am Chicago; 15:00Z is 10:00am Chicago.
    transport.availabilities = [_friday_slot(13), _friday_slot(15)]
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    verdict = await adapter.check_availability(
        _ctx(
            start=datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc),
            title="Swedish massage",
            service_description="Swedish massage",
        )
    )

    assert not verdict.available
    assert "outside_business_hours" in (verdict.reason or "")
    assert [slot["start"] for slot in verdict.alternatives] == ["2026-10-02T15:00:00Z"]


@pytest.mark.asyncio
async def test_day_part_openings_drop_times_before_dashboard_open(monkeypatch):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        timezone="America/Chicago",
        business_hours={"fri": [{"open": "10:00", "close": "18:00"}]},
        services=[{
            "name": "Swedish massage",
            "duration_minutes": 60,
            "square_variation_id": "var_massage",
            "square_variation_version": 1,
        }],
    )
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.availabilities = [_friday_slot(13), _friday_slot(15)]
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    slots = await adapter.list_openings(
        _ctx(
            start=datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 2, 14, 0, tzinfo=timezone.utc),
            title="Swedish massage",
            service_description="Swedish massage",
        ),
        datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 2, 22, 0, tzinfo=timezone.utc),
    )

    assert [slot["start"] for slot in slots] == ["2026-10-02T15:00:00Z"]


@pytest.mark.asyncio
async def test_massage_is_searched_only_on_six_calendar(monkeypatch):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        timezone="America/Chicago",
        business_hours={"fri": [{"open": "10:00", "close": "18:00"}]},
        services=[{
            "name": "Swedish massage",
            "duration_minutes": 60,
            "square_variation_id": "var_massage",
            "square_variation_version": 1,
        }],
        staff=[
            {"name": "SIX", "services": ["Facials", "Massages", "Back treatments"]},
            {"name": "Alex", "services": ["Nails"]},
        ],
    )
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.team_members = [
        {"id": "TM_SIX", "given_name": "SIX", "family_name": ""},
        {"id": "TM_ALEX", "given_name": "Alex", "family_name": ""},
    ]
    transport.availabilities = [_friday_slot(15, "TM_SIX")]
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    verdict = await adapter.check_availability(
        _ctx(
            start=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
            title="Swedish massage",
            service_description="Swedish massage",
        )
    )

    assert verdict.available
    assert verdict.slot["team_member_id"] == "TM_SIX"
    search = transport.calls_to("/v2/bookings/availability/search")[0]
    segment = search["query"]["filter"]["segment_filters"][0]
    assert segment["team_member_id_filter"] == {"any": ["TM_SIX"]}


@pytest.mark.asyncio
async def test_unassigned_provider_is_not_booked_for_a_restricted_service(monkeypatch):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        services=[{"name": "Swedish massage", "duration_minutes": 60}],
        staff=[
            {"name": "SIX", "services": ["Massages"]},
            {"name": "Alex", "services": ["Nails"]},
        ],
    )
    adapter = SpaBookingAdapter(spa)

    async def _reject(*_args, **_kwargs):
        raise AssertionError("Square must not be searched for a provider who does not do this service")

    monkeypatch.setattr(adapter.delegate, "_request", _reject)
    verdict = await adapter.check_availability(
        _ctx(preferred_staff="Alex", title="Swedish massage", service_description="Swedish massage")
    )
    assert not verdict.available
    assert "SIX" in (verdict.reason or "")
    assert "Alex" in (verdict.reason or "")


@pytest.mark.asyncio
async def test_unassigned_service_is_not_blocked_by_other_assignments(monkeypatch):
    spa = make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "t", "location_id": "loc_123"},
        timezone="America/Chicago",
        business_hours={"fri": [{"open": "08:00", "close": "18:00"}]},
        services=[{
            "name": "Gel nails",
            "duration_minutes": 60,
            "square_variation_id": "var_nails",
            "square_variation_version": 1,
        }],
        staff=[{"name": "SIX", "services": ["Massages"]}],
    )
    adapter = SpaBookingAdapter(spa)
    transport = _FakeSquareTransport()
    transport.availabilities = [_friday_slot(15, "TM_ALEX")]
    transport.availabilities[0]["appointment_segments"][0]["service_variation_id"] = "var_nails"
    monkeypatch.setattr(adapter.delegate, "_request", transport)

    verdict = await adapter.check_availability(
        _ctx(
            start=datetime(2026, 10, 2, 15, 0, tzinfo=timezone.utc),
            end=datetime(2026, 10, 2, 16, 0, tzinfo=timezone.utc),
            title="Gel nails",
            service_description="Gel nails",
        )
    )
    assert verdict.available
    search = transport.calls_to("/v2/bookings/availability/search")[0]
    assert "team_member_id_filter" not in search["query"]["filter"]["segment_filters"][0]
