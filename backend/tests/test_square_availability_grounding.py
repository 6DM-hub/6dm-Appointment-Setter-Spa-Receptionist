"""Square availability must be spoken only from a completed provider result."""
import asyncio
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import CardStatus
from app.services.appointment_booking_service import (
    AvailabilityResult,
    BookingOutcome,
    check_availability_only,
    confirm_booking,
    stage_booking,
)
from app.services.booking_adapters.base import ExternalBooking
from app.services.booking_adapters.base import AvailabilityVerdict, BookingContext, BookingProviderError
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.booking_state import (
    arm_verified_proposal,
    contains_unauthorized_availability_claim,
    get_draft,
    grounded_availability_speech,
    invalidate_booking_proposal,
    remember_verified_availability,
    save_draft,
    stage,
)
from app.services.business_hours import resolve_timezone
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from app.services.media_bridge import TwilioMediaBridge, mulaw_silence
from app.services.xai_realtime import XAIVoiceSession, cached_availability_output


TZ = resolve_timezone("America/Chicago")
START = datetime(2026, 10, 2, 20, 0, tzinfo=timezone.utc)  # 3:00 PM CDT
END = START + timedelta(hours=1)


def _session() -> CallSession:
    session = CallSession(
        "CA123",
        "inbound",
        "+15551110001",
        "+15552220000",
        tenant_id=str(uuid.uuid4()),
    )
    session.entities["booking_provider"] = "square"
    session.timezone = "America/Chicago"
    return session


def _slot(start: datetime, variation: str = "VAR") -> dict:
    return {
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "location_id": "LOC",
        "team_member_id": "TM",
        "service_variation_id": variation,
        "duration_minutes": 60,
    }


def test_unverified_claim_is_blocked_before_any_square_result():
    session = _session()
    session.requested_datetime = START.isoformat()
    assert contains_unauthorized_availability_claim("3 PM is available.", session, TZ)
    assert not contains_unauthorized_availability_claim(
        "You'd like 3 PM, let me check on that.", session, TZ
    )


def test_exact_square_slot_may_be_offered():
    session = _session()
    remember_verified_availability(
        session,
        [_slot(START)],
        service="Swedish massage",
        staff=None,
        location_id="LOC",
        duration_minutes=60,
        date_iso="2026-10-02",
        source="exact_check",
    )
    assert not contains_unauthorized_availability_claim("3 PM is available.", session, TZ)


def test_repeat_uses_the_verified_slots_not_a_new_list():
    session = _session()
    later = START + timedelta(minutes=60)
    remember_verified_availability(
        session,
        [_slot(START), _slot(later)],
        service="Swedish massage",
        staff=None,
        location_id="LOC",
        duration_minutes=60,
        date_iso="2026-10-02",
        source="alternative_search",
    )
    speech = grounded_availability_speech(session, TZ)
    assert speech == "I have 3 PM and 4 PM."


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("requested_start_iso", "2026-10-03T20:00:00+00:00", "date_changed"),
        ("service_description", "Facial", "service_changed"),
        ("preferred_staff", "Amy", "staff_changed"),
    ],
)
def test_context_change_invalidates_verified_availability(field, value, reason, caplog):
    session = _session()
    intent = AppointmentIntent(
        confidence=1.0,
        intent="schedule",
        service_description="Swedish massage",
        requested_start_iso=START.isoformat(),
    )
    stage(session, intent)
    remember_verified_availability(
        session,
        [_slot(START)],
        service="Swedish massage",
        staff=None,
        location_id="LOC",
        duration_minutes=60,
        date_iso="2026-10-02",
        source="exact_check",
    )
    setattr(intent, field, value)
    with caplog.at_level("INFO", logger="receptionist.truth"):
        stage(session, intent)
    draft = get_draft(session)
    assert draft.verified_availability is None
    assert f"reason={reason}" in caplog.text


def test_explicit_invalidate_clears_verified_slots():
    session = _session()
    remember_verified_availability(
        session,
        [_slot(START)],
        service="Swedish massage",
        staff=None,
        location_id="LOC",
        duration_minutes=60,
        date_iso="2026-10-02",
        source="exact_check",
    )
    invalidate_booking_proposal(session, "location_changed")
    assert get_draft(session).verified_availability is None


class _Square(SquareAdapter):
    def __init__(self) -> None:
        super().__init__("Spa", {"access_token": "token", "location_id": "LOC"})
        self.timezone_name = "America/Chicago"
        self.searches: list[str] = []

    async def _verify_pinned_slot(self, ctx):
        self.searches.append("exact")
        self._last_range_slots = []
        return None

    async def _search_square_availabilities(self, ctx, range_start, range_end):
        self.searches.append("exact")
        self._last_availability_latency_ms = 5
        return [], "VAR", []

    async def list_openings(self, ctx, range_start, range_end):
        self.searches.append("day")
        return [
            _slot(START + timedelta(hours=1)),
            _slot(START + timedelta(minutes=90)),
            _slot(START + timedelta(hours=2, minutes=15)),
        ]


@pytest.mark.asyncio
async def test_exact_miss_searches_the_day_for_real_alternatives():
    adapter = _Square()
    verdict = await adapter.check_availability(
        BookingContext(
            start=START,
            end=END,
            title="Swedish massage",
            customer_phone="+15550001",
            service_description="Swedish massage",
        )
    )
    assert adapter.searches == ["exact", "day"]
    assert verdict.available is False
    assert len(verdict.alternatives) == 3
    assert verdict.alternatives[0]["start"].startswith("2026-10-02T21:00:00")


@pytest.mark.asyncio
async def test_zero_square_slots_do_not_invent_times():
    adapter = _Square()

    async def _empty(ctx, range_start, range_end):
        adapter.searches.append("day")
        return []

    adapter.list_openings = _empty
    verdict = await adapter.check_availability(
        BookingContext(start=START, end=END, title="Swedish", customer_phone="+1", service_description="Swedish")
    )
    assert verdict.available is False
    assert verdict.alternatives == ()


@pytest.mark.asyncio
async def test_pinned_recheck_does_not_substitute_another_slot():
    adapter = _Square()
    verdict = await adapter.check_availability(
        BookingContext(
            start=START,
            end=END,
            title="Swedish",
            customer_phone="+1",
            service_description="Swedish",
            selected_slot=_slot(START),
        )
    )
    assert adapter.searches == ["exact"]
    assert verdict.available is False
    assert verdict.alternatives == ()


@pytest.mark.asyncio
async def test_http_200_with_no_slots_is_unavailable_not_a_lookup_failure(monkeypatch):
    session = _session()
    spa = type("Spa", (), {"id": "spa", "timezone": "America/Chicago"})()

    class _Adapter:
        provider = "square"
        default_duration_minutes = 60

        async def booking_timezone_name(self):
            return "America/Chicago"

        async def check_availability(self, ctx):
            return AvailabilityVerdict.no(
                "The requested appointment time is not available in Square",
                alternatives=(),
            )

    monkeypatch.setattr(
        "app.services.appointment_booking_service._load_spa",
        lambda *_: _async(spa),
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service.get_booking_adapter",
        lambda **_: _Adapter(),
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service._has_conflict",
        lambda *_a, **_k: _async(False),
    )
    result = await check_availability_only(
        None,
        session,
        AppointmentIntent(
            confidence=1.0,
            intent="inquiry",
            requested_start_iso="2026-10-02T15:00:00",
            service_description="Swedish massage",
        ),
    )
    assert result.available is False
    assert result.lookup_failed is False
    assert "trouble checking" not in result.message.lower()
    assert "do not invent" in result.message.lower()


@pytest.mark.asyncio
async def test_square_timeout_is_not_reported_as_unavailable(monkeypatch):
    session = _session()
    spa = type("Spa", (), {"id": "spa", "timezone": "America/Chicago"})()

    class _Adapter:
        provider = "square"
        default_duration_minutes = 60

        async def booking_timezone_name(self):
            return "America/Chicago"

        async def check_availability(self, ctx):
            raise BookingProviderError("timeout", retryable=True)

    monkeypatch.setattr(
        "app.services.appointment_booking_service._load_spa",
        lambda *_: _async(spa),
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service.get_booking_adapter",
        lambda **_: _Adapter(),
    )
    result = await check_availability_only(
        None,
        session,
        AppointmentIntent(
            confidence=1.0,
            intent="inquiry",
            requested_start_iso="2026-10-02T15:00:00",
            service_description="Swedish massage",
        ),
    )
    assert isinstance(result, AvailabilityResult)
    assert result.lookup_failed is True
    assert result.message == "I'm having trouble checking the schedule right now."
    assert "unavailable" not in result.message.lower()


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_duplicate_identical_availability_request_is_not_run_twice():
    import time

    session = _session()
    voice = XAIVoiceSession("CA123", session)
    raw = '{"requested_start_iso":"2026-10-02T15:00:00"}'
    cached = '{"status":"available","spoken":"3 PM is available."}'
    voice._availability_recent[raw] = (time.monotonic(), cached)
    calls = {"n": 0}

    async def _check(_db, _session, _intent):
        calls["n"] += 1
        return AvailabilityResult(True, "3 PM is available.")

    import app.services.xai_realtime as realtime

    original = realtime.check_availability_only
    realtime.check_availability_only = _check
    try:
        replayed = await voice._run_check_availability(raw)
    finally:
        realtime.check_availability_only = original
    assert calls["n"] == 0
    assert replayed == cached


@pytest.mark.asyncio
async def test_blocked_availability_claim_speaks_instead_of_going_silent():
    session = _session()
    voice = XAIVoiceSession("CA123", session)
    sent: list[dict] = []

    async def _send(payload):
        sent.append(payload)

    voice._send = _send
    voice._active_response_id = "resp-1"
    remember_verified_availability(
        session,
        [_slot(START + timedelta(hours=1))],
        service="Swedish massage",
        staff=None,
        location_id="LOC",
        duration_minutes=60,
        date_iso="2026-10-02",
        source="alternative_search",
    )
    await voice._block_unverified_availability(
        "The earliest available times tomorrow are 3 PM, 4:15 PM, and 5:30 PM."
    )
    assert any(item.get("type") == "response.cancel" for item in sent)
    spoken = [
        item for item in sent
        if item.get("type") == "conversation.item.create"
    ]
    assert spoken
    assert "4 PM" in str(spoken[-1])


def test_revalidation_source_never_uses_the_three_second_cache():
    recent = {"same": (time.monotonic(), '{"status":"available","spoken":"3 PM is available."}')}
    assert cached_availability_output(recent, "same", time.monotonic(), source="revalidation") is None
    assert cached_availability_output(recent, "same", time.monotonic(), source="exact_check") is not None


@pytest.mark.asyncio
async def test_full_day_alternatives_are_ranked_by_closeness_not_later_only():
    """2:30 is closer to 3:00 than 4:00 or 5:30, so it is offered first.

    The forward search already found 4:00. That must not suppress the same-day
    search or hide the earlier Square slot.
    """
    adapter = _Square()
    later = START + timedelta(hours=1)

    async def _search(ctx, range_start, range_end):
        adapter.searches.append("exact")
        adapter._last_availability_latency_ms = 5
        return [{
            "start_at": later.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "location_id": "LOC",
            "appointment_segments": [{
                "team_member_id": "TM",
                "service_variation_id": "VAR",
                "duration_minutes": 60,
            }],
        }], "VAR", []

    async def _day(ctx, range_start, range_end):
        adapter.searches.append("day")
        return [
            _slot(START - timedelta(hours=6)),
            _slot(START - timedelta(minutes=30)),
            _slot(START + timedelta(hours=1)),
            _slot(START + timedelta(hours=2, minutes=30)),
        ]

    adapter._search_square_availabilities = _search
    adapter.list_openings = _day
    verdict = await adapter.check_availability(
        BookingContext(
            start=START,
            end=END,
            title="Swedish",
            customer_phone="+1",
            service_description="Swedish",
        )
    )
    assert adapter.searches == ["exact", "day"]
    assert [slot["start"] for slot in verdict.alternatives] == [
        "2026-10-02T19:30:00Z",
        "2026-10-02T21:00:00Z",
        "2026-10-02T22:30:00Z",
    ]


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


class _SelectingCalendar:
    provider = "square"
    calendar_label = "Square"
    default_title = "Appointment"
    default_duration_minutes = 60

    def __init__(self, *, revalidation_available: bool) -> None:
        self.revalidation_available = revalidation_available
        self.pinned_checks = 0
        self.created: list[datetime] = []
        self.offered: list[str] = []

    async def booking_timezone_name(self):
        return "America/Chicago"

    async def check_availability(self, ctx):
        if ctx.selected_slot:
            self.pinned_checks += 1
            if not self.revalidation_available:
                return AvailabilityVerdict.no(
                    "The requested appointment time is not available in Square"
                )
        return AvailabilityVerdict.ok(slot=_slot(ctx.start))

    async def list_openings(self, ctx, range_start, range_end):
        slots = [
            _slot(START - timedelta(hours=6)),
            _slot(START - timedelta(minutes=30)),
            _slot(START + timedelta(hours=1)),
            _slot(START + timedelta(hours=2, minutes=30)),
        ]
        self.offered = [slot["start"] for slot in slots]
        return slots

    async def create_booking(self, ctx):
        self.created.append(ctx.start)
        return ExternalBooking(provider="square", external_id="sq_1")


def _patch_booking(monkeypatch, calendar):
    spa = SimpleNamespace(id=uuid.uuid4(), timezone="America/Chicago", staff=[])
    monkeypatch.setattr(
        "app.services.appointment_booking_service._load_spa",
        lambda *_: _async(spa),
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service.get_booking_adapter",
        lambda **_: calendar,
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service._has_conflict",
        lambda *_a, **_k: _async(False),
    )
    for name in ("_load_by_intent_key", "_load_active_appointment", "_load_call_owned_appointment", "_load_call_log_id"):
        monkeypatch.setattr(
            f"app.services.appointment_booking_service.{name}",
            lambda *_a, **_k: _async(None),
        )
    monkeypatch.setattr(
        "app.services.appointment_booking_service._resolve_contact",
        lambda *_a, **_k: _async(SimpleNamespace(id=uuid.uuid4())),
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service.card_status_for_booked_appointment",
        lambda **_k: _async(CardStatus.NOT_REQUIRED),
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service.upsert_external_customer_link",
        lambda *_a, **_k: _async(None),
    )


async def _stage_three_pm(calendar):
    session = _session()
    staged = await stage_booking(
        _FakeDB(),
        session,
        AppointmentIntent(
            confidence=1.0,
            intent="schedule",
            requested_start_iso=START.isoformat(),
            service_description="Swedish massage",
        ),
    )
    assert staged.outcome is BookingOutcome.DRAFT
    named = get_draft(session)
    named.caller_name = "Ada"
    save_draft(session, named)
    remember_verified_availability(
        session,
        [_slot(START)],
        service="Swedish massage",
        staff=None,
        location_id="LOC",
        duration_minutes=60,
        date_iso="2026-10-02",
        source="exact_check",
    )
    arm_verified_proposal(session)
    return session


@pytest.mark.asyncio
async def test_selecting_a_verified_slot_rechecks_square_before_booking(monkeypatch):
    calendar = _SelectingCalendar(revalidation_available=True)
    _patch_booking(monkeypatch, calendar)
    session = await _stage_three_pm(calendar)
    assert get_draft(session).verified_availability is not None
    confirmed = await confirm_booking(_FakeDB(), session)
    assert calendar.pinned_checks == 1
    assert confirmed.outcome is BookingOutcome.BOOKED
    assert len(calendar.created) == 1


@pytest.mark.asyncio
async def test_unavailable_revalidation_does_not_book_and_offers_new_square_slots(monkeypatch):
    calendar = _SelectingCalendar(revalidation_available=False)
    _patch_booking(monkeypatch, calendar)
    session = await _stage_three_pm(calendar)
    confirmed = await confirm_booking(_FakeDB(), session)
    assert confirmed.outcome is BookingOutcome.CONFLICT
    assert calendar.created == []
    assert calendar.pinned_checks == 1
    assert "2026-10-02T14:00:00" not in confirmed.message
    assert "2026-10-02T19:30:00" in confirmed.message
    assert "2026-10-02T21:00:00" in confirmed.message
    assert "2026-10-02T22:30:00" in confirmed.message
    labels = get_draft(session).verified_availability["slots"]
    assert [slot["start"] for slot in labels] == [
        "2026-10-02T19:30:00Z",
        "2026-10-02T21:00:00Z",
        "2026-10-02T22:30:00Z",
    ]


@pytest.mark.asyncio
async def test_context_change_during_lookup_discards_the_square_result(monkeypatch):
    session = _session()
    stage(
        session,
        AppointmentIntent(
            confidence=1.0,
            intent="schedule",
            requested_start_iso=START.isoformat(),
            service_description="Swedish massage",
        ),
    )
    remember_verified_availability(
        session,
        [_slot(START)],
        service="Swedish massage",
        staff=None,
        location_id="LOC",
        duration_minutes=60,
        date_iso="2026-10-02",
        source="exact_check",
    )
    spa = SimpleNamespace(id="spa", timezone="America/Chicago")

    class _Adapter:
        provider = "square"
        default_duration_minutes = 60

        async def booking_timezone_name(self):
            return "America/Chicago"

        async def check_availability(self, ctx):
            stage(
                session,
                AppointmentIntent(
                    confidence=1.0,
                    intent="schedule",
                    requested_start_iso="2026-10-03T20:00:00+00:00",
                    service_description="Swedish massage",
                ),
            )
            return AvailabilityVerdict.ok(slot=_slot(START))

    monkeypatch.setattr(
        "app.services.appointment_booking_service._load_spa",
        lambda *_: _async(spa),
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service.get_booking_adapter",
        lambda **_: _Adapter(),
    )
    monkeypatch.setattr(
        "app.services.appointment_booking_service._has_conflict",
        lambda *_a, **_k: _async(False),
    )
    result = await check_availability_only(
        None,
        session,
        AppointmentIntent(
            confidence=1.0,
            intent="inquiry",
            requested_start_iso=START.isoformat(),
            service_description="Swedish massage",
        ),
    )
    assert result.stale is True
    draft = get_draft(session)
    assert draft.verified_availability is None
    assert draft.start_iso.startswith("2026-10-03")


@pytest.mark.asyncio
async def test_voice_layer_does_not_speak_a_stale_availability_result(monkeypatch):
    session = _session()
    voice = XAIVoiceSession("CA123", session)
    voice._persist_session = _async_none

    async def _check(_db, _session, _intent):
        draft = get_draft(session)
        draft.draft_revision += 1
        save_draft(session, draft)
        return AvailabilityResult(True, "3 PM is available. Offer only this Square time.")

    monkeypatch.setattr("app.services.xai_realtime.check_availability_only", _check)
    monkeypatch.setattr("app.services.xai_realtime.AsyncSessionLocal", _db_cm)
    output = await voice._run_check_availability('{"requested_start_iso":"2026-10-02T15:00:00"}')
    assert '"spoken"' not in output
    assert "stale" in output


@pytest.mark.asyncio
async def test_hold_phrase_is_audible_before_square_and_result_follows(monkeypatch):
    session = _session()
    voice = XAIVoiceSession("CA123", session)
    order: list[str] = []

    async def _send(payload):
        item = payload.get("item") or {}
        content = item.get("content") or [{}]
        text = str((content[0] or {}).get("text") or "") if content else ""
        if text:
            order.append(text)

    voice.session.add_turn("user", "October 2 at 3 PM")
    voice._user_turn_count = 1
    voice._send = _send
    voice._ws = object()
    voice._active_response_id = "model-1"
    voice._persist_session = _async_none
    release = asyncio.Event()
    started = asyncio.Event()

    async def _check(_db, _session, _intent):
        started.set()
        await release.wait()
        return AvailabilityResult(True, "3 PM is available. Offer only this Square time.")

    monkeypatch.setattr("app.services.xai_realtime.check_availability_only", _check)
    monkeypatch.setattr("app.services.xai_realtime.AsyncSessionLocal", _db_cm)
    task = asyncio.create_task(voice._handle_function_call({
        "type": "response.function_call_arguments.done",
        "name": "check_availability",
        "call_id": "call-1",
        "arguments": '{"requested_start_iso":"2026-10-02T15:00:00","service_description":"Swedish"}',
    }))
    await asyncio.wait_for(started.wait(), timeout=2)
    assert order == ["Let me check that for you."]
    assert voice._muted_availability_response_id == "model-1"
    await voice._dispatch({"type": "response.created", "response": {"id": "hold-1"}})
    release.set()
    await task
    assert order == ["Let me check that for you."]
    assert voice._pending_availability_speech and "3 PM" in voice._pending_availability_speech
    await voice._dispatch({"type": "response.done", "response": {"id": "hold-1"}})
    assert order[0] == "Let me check that for you."
    assert "3 PM is available" in order[-1]


@pytest.mark.asyncio
async def test_lookup_mute_drops_model_audio_and_keeps_the_hold_phrase():
    class _WS:
        async def send_text(self, _text):
            return None

        async def receive_text(self):
            return "{}"

    bridge = TwilioMediaBridge("CA123", _session(), _WS())
    bridge._allow_audio = True
    bridge._muted_availability_response_id = "model-1"
    queued: list[str] = []

    def _enqueue(delta: str) -> bool:
        queued.append(delta)
        return True

    bridge._enqueue_audio = _enqueue
    await bridge._dispatch({
        "type": "response.output_audio.delta",
        "response_id": "model-1",
        "delta": mulaw_silence(),
    })
    await bridge._dispatch({
        "type": "response.output_audio.delta",
        "response_id": "hold-1",
        "delta": mulaw_silence(),
    })
    assert queued == [mulaw_silence()]


@pytest.mark.asyncio
async def test_square_lookup_does_not_block_the_audio_pump():
    class _WS:
        def __init__(self):
            self._events = [
                '{"type":"response.function_call_arguments.done","name":"check_availability","call_id":"c1","arguments":"{}"}',
                '{"type":"response.output_audio.delta","response_id":"hold-1","delta":"%s"}' % mulaw_silence(),
            ]

        async def recv(self):
            if self._events:
                return self._events.pop(0)
            await asyncio.Event().wait()

    class _Socket:
        async def send_text(self, _text):
            return None

    bridge = TwilioMediaBridge("CA123", _session(), _Socket())
    bridge._ws = _WS()
    bridge._allow_audio = True
    gate = asyncio.Event()
    heard = asyncio.Event()
    original = bridge._dispatch

    async def _dispatch(event):
        if event.get("type") == "response.function_call_arguments.done":
            await gate.wait()
            return
        await original(event)
        if event.get("type") == "response.output_audio.delta":
            heard.set()

    bridge._dispatch = _dispatch
    pump = asyncio.create_task(bridge._pump_xai_to_twilio())
    await asyncio.wait_for(heard.wait(), timeout=2)
    gate.set()
    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump


def _db_cm():
    class _CM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    return _CM()


async def _async_none():
    return None


ELEVEN_THIRTY = datetime(2026, 10, 3, 16, 30, tzinfo=timezone.utc)
ELEVEN_FIFTEEN = datetime(2026, 10, 3, 16, 15, tzinfo=timezone.utc)
TWELVE_THIRTY = datetime(2026, 10, 3, 17, 30, tzinfo=timezone.utc)
ONE_FORTY_FIVE = datetime(2026, 10, 3, 18, 45, tzinfo=timezone.utc)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(microsecond=0)


class _ElevenCalendar:
    provider = "square"
    calendar_label = "Square"
    default_title = "Appointment"
    default_duration_minutes = 60

    def __init__(self, *, eleven_fifteen_available: bool) -> None:
        self.eleven_fifteen_available = eleven_fifteen_available
        self.checks: list[datetime] = []
        self.created: list[datetime] = []
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.block_next = False

    async def booking_timezone_name(self):
        return "America/Chicago"

    async def check_availability(self, ctx):
        moment = _as_utc(ctx.start)
        self.checks.append(moment)
        if self.block_next:
            self.block_next = False
            self.started.set()
            await self.release.wait()
        if moment == ELEVEN_FIFTEEN and not self.eleven_fifteen_available:
            return AvailabilityVerdict.no(
                "The requested appointment time is not available in Square",
                alternatives=(_slot(TWELVE_THIRTY), _slot(ONE_FORTY_FIVE)),
            )
        if moment in {ELEVEN_FIFTEEN, ELEVEN_THIRTY}:
            return AvailabilityVerdict.ok(slot=_slot(moment))
        return AvailabilityVerdict.no("That time is not available in Square")

    async def list_openings(self, ctx, range_start, range_end):
        return [_slot(TWELVE_THIRTY), _slot(ONE_FORTY_FIVE)]

    async def create_booking(self, ctx):
        self.created.append(_as_utc(ctx.start))
        return ExternalBooking(provider="square", external_id="sq_1115")


def _voice_for_proposal(monkeypatch, calendar):
    _patch_booking(monkeypatch, calendar)
    monkeypatch.setattr(
        "app.services.appointment_booking_service.persist_caller_identity",
        lambda *_a, **_k: _async(None),
    )

    async def _stage(_db, session, intent):
        return await stage_booking(_FakeDB(), session, intent)

    monkeypatch.setattr("app.services.xai_realtime.stage_booking", _stage)
    session = _session()
    voice = XAIVoiceSession("CA123", session)
    sent: list[dict] = []

    async def _send(payload):
        sent.append(payload)

    voice._send = _send
    voice._ws = object()
    voice._active_response_id = "model-1"
    voice._persist_session = _async_none
    voice.HOLD_ACK_DELAY_SECONDS = 30
    return voice, sent


async def _propose(voice, iso: str, *, caller_name: str | None = None, call_id: str = "propose-1"):
    args = {
        "requested_start_iso": iso,
        "service_description": "60 minute Swedish massage",
    }
    if caller_name:
        args["caller_name"] = caller_name
    return asyncio.create_task(voice._handle_function_call({
        "name": "propose_appointment",
        "call_id": call_id,
        "arguments": json.dumps(args),
    }))


def _texts(sent: list[dict]) -> list[str]:
    texts = []
    for payload in sent:
        content = ((payload.get("item") or {}).get("content") or [{}])
        text = str((content[0] or {}).get("text") or "") if content else ""
        if text:
            texts.append(text)
    return texts


@pytest.mark.asyncio
async def test_time_change_proposal_is_spoken_by_the_server_then_books_once(monkeypatch):
    calendar = _ElevenCalendar(eleven_fifteen_available=True)
    voice, sent = _voice_for_proposal(monkeypatch, calendar)
    await stage_booking(
        _FakeDB(),
        voice.session,
        AppointmentIntent(
            confidence=1.0,
            intent="schedule",
            requested_start_iso="2026-10-03T11:30:00",
            service_description="60 minute Swedish massage",
        ),
    )
    assert "11:30:00" in (get_draft(voice.session).start_iso or "")
    voice.session.add_turn("user", "October 3 at 11:15 AM")
    voice._user_turn_count = 1
    calendar.block_next = True
    task = await _propose(voice, "2026-10-03T11:15:00")
    await asyncio.wait_for(calendar.started.wait(), timeout=2)
    assert _texts(sent) == ["Let me check that for you."]
    await voice._dispatch({"type": "response.created", "response": {"id": "hold-1"}})
    calendar.release.set()
    await task
    assert not any(item.get("type") == "response.create" for item in sent)
    await voice._dispatch({"type": "response.done", "response": {"id": "hold-1"}})
    spoken = _texts(sent)
    assert spoken[0] == "Let me check that for you."
    assert spoken[-1] == (
        "11:15 AM is available for 60 minute Swedish massage. "
        "Nothing is booked yet. Would you like me to book that?"
    )
    draft = get_draft(voice.session)
    assert "11:15:00" in (draft.start_iso or "")
    assert "11:30" not in (draft.start_iso or "")
    assert draft.selected_slot["start"] == "2026-10-03T16:15:00Z"
    assert draft.verified_availability is None or "11:30" not in str(draft.verified_availability)

    before_name = len(calendar.checks)
    voice._hold_ack_played_this_turn = False
    voice.session.add_turn("user", "My name is Rayn")
    voice._user_turn_count = 2
    name_task = await _propose(voice, "2026-10-03T11:15:00", caller_name="Rayn", call_id="propose-2")
    await name_task
    assert len(calendar.checks) > before_name
    assert calendar.created == []
    assert get_draft(voice.session).caller_name == "Rayn"

    arm_verified_proposal(voice.session)
    confirmed = await confirm_booking(_FakeDB(), voice.session)
    assert confirmed.outcome is BookingOutcome.BOOKED
    assert calendar.created == [ELEVEN_FIFTEEN]


@pytest.mark.asyncio
async def test_unavailable_proposal_speaks_only_square_alternatives(monkeypatch):
    calendar = _ElevenCalendar(eleven_fifteen_available=False)
    voice, sent = _voice_for_proposal(monkeypatch, calendar)
    voice.session.add_turn("user", "October 3 at 11:15 AM")
    voice._user_turn_count = 1
    calendar.block_next = True
    task = await _propose(voice, "2026-10-03T11:15:00")
    await asyncio.wait_for(calendar.started.wait(), timeout=2)
    await voice._dispatch({"type": "response.created", "response": {"id": "hold-1"}})
    calendar.release.set()
    await task
    await voice._dispatch({"type": "response.done", "response": {"id": "hold-1"}})
    assert _texts(sent)[-1] == "11:15 AM isn't available, but I have 12:30 PM and 1:45 PM."
    assert not any(item.get("type") == "response.create" for item in sent)
    assert calendar.created == []
    assert get_draft(voice.session).selected_slot is None
