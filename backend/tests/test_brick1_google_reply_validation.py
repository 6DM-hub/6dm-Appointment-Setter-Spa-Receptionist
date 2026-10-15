"""Brick 1, finding R5-F1: a Google move is only a success if Google's reply proves it.

Before this repair, ``update_event`` awaited Google's PATCH and discarded the
reply, so an HTTP-successful answer that contradicted the move (the old time,
another event's id, a cancelled event) was reported as a verified move and the
local appointment was changed to match. These regressions pin the repair on
every path that reaches a Google calendar:

* the service function (``update_event``),
* the dashboard route (``update_appointment``),
* the realtime voice path and the legacy Twilio path, through the real booking
  engine, for several unrelated businesses (nothing here is spa specific).

SIMULATED PROVIDER ONLY. Only the Google client transport (``google_calendar._service``)
is replaced; the service, adapter, booking engine, dashboard route, realtime
dispatcher and legacy handler are the real code. This is not a test against
real Google, and it does not establish how often Google really sends such
replies.

For every contradictory reply the required behaviour is the same:

1. the caller or staff member is NOT told the move happened;
2. the local appointment keeps its original time;
3. the outcome is recorded as unknown (the durable unresolved-change record),
   which blocks the next change of that appointment;
4. a retry makes NO second call to Google.

A valid reply is the positive control: the move succeeds and nothing is left
unresolved.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.appointments import update_appointment
from app.models import BookingProvider
from app.schemas import AppointmentUpdate
from app.services import appointment_booking_service as booking
from app.services import google_calendar as gc
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
from app.services.google_calendar import GoogleCalendarError
from tests import square_world
from tests.square_world import call, local, square_spa, voice_for, wire
from tests.test_brick1_booking_change_guard import (
    BUSINESSES, NOW, Phone, LegacyCall, T2, T3, THREE, _intent,
)
from tests.test_cara_native_double_booking import rubyz_db  # noqa: F401  (fixture)
from tests.test_codex_brick1_tenant_boundaries import book, session_for, setup_tenants

CALENDAR = "dummy-calendar"
CONTRADICTORY = ["unchanged_time", "wrong_id", "cancelled"]
ALL_SHAPES = CONTRADICTORY + ["valid"]


class FakeGoogle:
    """A simulated Google Calendar client transport (what ``_service`` returns).

    ``events`` holds the calendar's TRUE state. A contradictory ``shape`` makes
    PATCH answer with a reply that disagrees with the request while the true
    state stays where it was.
    """

    def __init__(self, shape: str = "valid"):
        self.shape = shape
        self.state: dict[str, dict] = {}
        self.inserts: list[dict] = []
        self.patches: list[dict] = []
        self._next = 0

    # -- service object ------------------------------------------------------
    def service(self):
        return SimpleNamespace(
            events=lambda: SimpleNamespace(
                insert=lambda **kw: _Req(lambda: self._insert(kw)),
                patch=lambda **kw: _Req(lambda: self._patch(kw)),
                delete=lambda **kw: _Req(lambda: {}),
            ),
            freebusy=lambda: SimpleNamespace(
                query=lambda body: _Req(lambda: {"calendars": {CALENDAR: {"busy": []}}})
            ),
        )

    def _insert(self, kw):
        self.inserts.append(copy.deepcopy(kw))
        body = kw["body"]
        self._next += 1
        event = {"id": body.get("id") or f"evt{self._next}", "status": "confirmed",
                 "start": body["start"], "end": body["end"]}
        self.state[event["id"]] = event
        return copy.deepcopy(event)

    def _patch(self, kw):
        self.patches.append(copy.deepcopy(kw))
        event = self.state[kw["eventId"]]
        body = copy.deepcopy(kw["body"])
        if self.shape == "valid":
            event.update(body)
            return copy.deepcopy(event)
        reply = copy.deepcopy(event)             # true state is NOT changed
        if self.shape == "wrong_id":
            reply["id"] = "different-customers-event"
            reply.update(body)
        elif self.shape == "cancelled":
            reply["status"] = "cancelled"
            reply.update(body)
        return reply                              # "unchanged_time": the old times

    def start_of(self, event_id: str) -> str:
        return self.state[event_id]["start"]["dateTime"]


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


def _install(monkeypatch, shape: str) -> tuple[FakeGoogle, GoogleCalendarAdapter, SimpleNamespace]:
    fake = FakeGoogle(shape)
    connection = SimpleNamespace(selected_calendar_id=CALENDAR)

    async def _service(conn):
        assert conn is connection
        return fake.service()

    monkeypatch.setattr(gc, "_service", _service)
    adapter = GoogleCalendarAdapter(calendar_id=CALENDAR, connection=connection)
    return fake, adapter, connection


# =========================================================================== #
# 1. The service function
# =========================================================================== #
def _times(start: datetime, hours: int = 1):
    return gc._event_times(start, start + timedelta(hours=hours), "America/Chicago")


async def _patch_with_reply(monkeypatch, reply):
    connection = SimpleNamespace(selected_calendar_id=CALENDAR)
    sent = []

    class Req:
        def execute(self):
            return copy.deepcopy(reply)

    service = SimpleNamespace(events=lambda: SimpleNamespace(
        patch=lambda **kw: (sent.append(kw), Req())[1]))

    async def _service(_conn):
        return service

    monkeypatch.setattr(gc, "_service", _service)
    await gc.update_event(connection, "evt-1", start=T3, end=T3 + timedelta(hours=1),
                          timezone_name="America/Chicago")
    return sent


def _good_reply(**overrides):
    start, end = _times(T3)
    reply = {"id": "evt-1", "status": "confirmed", "start": start, "end": end}
    reply.update(overrides)
    return reply


async def test_valid_reply_is_accepted(monkeypatch):
    sent = await _patch_with_reply(monkeypatch, _good_reply())
    assert len(sent) == 1 and sent[0]["eventId"] == "evt-1"


@pytest.mark.parametrize("status", ["confirmed", "tentative"])
async def test_active_statuses_are_accepted(monkeypatch, status):
    await _patch_with_reply(monkeypatch, _good_reply(status=status))


async def test_same_instants_in_other_spellings_are_accepted(monkeypatch):
    """The reply is compared as instants, not as text: UTC 'Z' and a different
    offset describe the same moment."""
    start_utc = T3.astimezone(timezone.utc)
    reply = _good_reply(
        start={"dateTime": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")},
        end={"dateTime": (start_utc + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S+00:00")},
    )
    await _patch_with_reply(monkeypatch, reply)


async def test_naive_reply_time_with_its_time_zone_is_accepted(monkeypatch):
    reply = _good_reply(
        start={"dateTime": "2026-10-22T15:00:00", "timeZone": "America/Chicago"},
        end={"dateTime": "2026-10-22T16:00:00", "timeZone": "America/Chicago"},
    )
    await _patch_with_reply(monkeypatch, reply)


UNVERIFIABLE_REPLIES = {
    "unchanged_time": lambda: _good_reply(start=_times(T2)[0], end=_times(T2)[1]),
    "wrong_id": lambda: _good_reply(id="another-event"),
    "cancelled": lambda: _good_reply(status="cancelled"),
    "missing_status": lambda: {k: v for k, v in _good_reply().items() if k != "status"},
    "missing_id": lambda: {k: v for k, v in _good_reply().items() if k != "id"},
    "missing_start": lambda: {k: v for k, v in _good_reply().items() if k != "start"},
    "missing_end": lambda: {k: v for k, v in _good_reply().items() if k != "end"},
    "start_wrong_end_right": lambda: _good_reply(start=_times(T2)[0]),
    "end_wrong_start_right": lambda: _good_reply(end=_times(T3, hours=2)[1]),
    "all_day_event": lambda: _good_reply(start={"date": "2026-10-22"}, end={"date": "2026-10-23"}),
    "unreadable_time": lambda: _good_reply(start={"dateTime": "not a time"}),
    "naive_time_without_zone": lambda: _good_reply(start={"dateTime": "2026-10-22T15:00:00"}),
    "empty_object": lambda: {},
    "no_object": lambda: None,
    "not_an_object": lambda: "ok",
}


@pytest.mark.parametrize("name", sorted(UNVERIFIABLE_REPLIES))
async def test_contradictory_or_unverifiable_reply_is_never_a_success(monkeypatch, name):
    with pytest.raises(GoogleCalendarError) as caught:
        await _patch_with_reply(monkeypatch, UNVERIFIABLE_REPLIES[name]())
    assert caught.value.status == "unverified_response"


async def test_adapter_reports_an_unverified_reply_as_an_unknown_outcome(monkeypatch):
    """Unknown, not a clean refusal: a refusal would let the caller retry."""
    from app.services.booking_adapters.base import BookingProviderError

    fake, adapter, _ = _install(monkeypatch, "cancelled")
    fake.state["evt-1"] = {"id": "evt-1", "status": "confirmed", "start": _times(T2)[0], "end": _times(T2)[1]}
    booking_ref = SimpleNamespace(provider="google_calendar", external_id="evt-1")
    with pytest.raises(BookingProviderError) as caught:
        await adapter.move_booking(booking_ref, T3, T3 + timedelta(hours=1))
    assert caught.value.code == "BOOKING_OUTCOME_UNKNOWN"
    assert caught.value.retryable is False
    assert booking.UNCERTAIN_OUTCOME_CODES >= {caught.value.code}


async def test_a_google_refusal_is_still_a_refusal_not_an_unknown(monkeypatch):
    """The repair does not blur the existing error classes."""
    from app.services.booking_adapters.base import BookingProviderError

    fake, adapter, _ = _install(monkeypatch, "valid")

    async def refuse(*_a, **_k):
        raise GoogleCalendarError("provider_error")

    monkeypatch.setattr("app.services.booking_adapters.google_calendar.update_event", refuse)
    with pytest.raises(BookingProviderError) as caught:
        await adapter.move_booking(SimpleNamespace(provider="google_calendar", external_id="evt-1"),
                                   T3, T3 + timedelta(hours=1))
    assert caught.value.code == "GOOGLE_REFUSED"
    assert caught.value.code not in booking.UNCERTAIN_OUTCOME_CODES


# =========================================================================== #
# 2. The dashboard route (real route, engine, adapter, service; SQLite)
# =========================================================================== #
@pytest.mark.parametrize("shape", ALL_SHAPES)
async def test_dashboard_move_is_proved_by_googles_reply(rubyz_db, monkeypatch, shape):
    db, tenant, other, squares = await setup_tenants(rubyz_db, monkeypatch)
    caller = session_for(tenant, f"r5f1-dash-{shape}")
    row = await book(db, tenant, caller)
    row.booking_provider = BookingProvider.GOOGLE_CALENDAR.value
    row.external_booking_id = "google-owned-event"
    await db.commit()
    prior_start, row_id, scope = row.start_time, row.id, caller.scope

    fake, adapter, _ = _install(monkeypatch, shape)
    fake.state["google-owned-event"] = {"id": "google-owned-event", "status": "confirmed",
                                        "start": _times(T2)[0], "end": _times(T2)[1]}
    monkeypatch.setattr(booking, "get_booking_adapter", lambda **_k: adapter)

    change = AppointmentUpdate(start_time=T3, end_time=T3 + timedelta(hours=1))
    error = None
    try:
        await update_appointment(row_id, change, scope=scope, db=db)
    except HTTPException as exc:
        error = exc
    await db.refresh(row)

    assert len(fake.patches) == 1 and fake.patches[0]["eventId"] == "google-owned-event"
    if shape == "valid":
        assert error is None
        assert row.start_time != prior_start
        assert fake.start_of("google-owned-event") == fake.patches[0]["body"]["start"]["dateTime"]
        return

    # Staff are told the outcome is unknown, never that the move happened.
    assert error is not None and error.status_code == 502, error
    assert "unknown" in str(error.detail).lower()
    assert row.start_time == prior_start, "the local mirror followed a move Google did not prove"
    # And the next attempt is stopped before it reaches Google a second time.
    retry_error = None
    try:
        await update_appointment(row_id, change, scope=scope, db=db)
    except HTTPException as exc:
        retry_error = exc
    assert retry_error is not None, "a retry was allowed after an unknown outcome"
    assert len(fake.patches) == 1, "an unsafe retry reached Google again"
    await db.refresh(row)
    assert row.start_time == prior_start


# =========================================================================== #
# 3. Voice: realtime and legacy paths, several unrelated businesses
# =========================================================================== #
def _google_world(monkeypatch, business: str, shape: str):
    menu = BUSINESSES[business]
    for item in menu:
        monkeypatch.setitem(square_world.VERSIONS, item["square_variation_id"], item["square_variation_version"])
        monkeypatch.setitem(square_world.MINUTES, item["square_variation_id"], item["duration_minutes"])
    fake, adapter, _ = _install(monkeypatch, shape)
    w = wire(monkeypatch, spa=square_spa(services=menu), adapter=adapter)
    w.google = fake
    w.monkeypatch = monkeypatch
    w.first, w.second = menu[0]["name"], menu[1]["name"]
    return w


def _the_appointment(w):
    rows = list(w.store.live)
    assert len(rows) == 1, rows
    return rows[0]


async def _realtime_booked(w) -> Phone:
    p = Phone(w)
    p.voice._pending_caller = f"I'd like a {w.first} on Thursday at 2 PM. My name is Ada Lovelace."
    p.voice._flush_caller_turn()
    out = await p.propose(requested_start_iso=local(T2), service_description=w.first,
                          caller_name="Ada Lovelace", operation="schedule")
    assert out["status"] == "draft", out
    await p.says("Yes, please.")
    assert len(w.google.inserts) == 1, p.heard
    return p


async def _legacy_booked_google(w) -> LegacyCall:
    c = LegacyCall(w, w.monkeypatch)
    await c.says(f"I'd like a {w.first} Thursday at 2 PM, I'm Ada Lovelace.",
                 intent=_intent("schedule", T2, w.first),
                 reply="Thursday at 2 PM is open. Would you like me to book that?")
    await c.says("Yes.", reply="You're all set. Is there anything else I can help you with today?")
    assert len(w.google.inserts) == 1
    return c


def _unknown_outcome_recorded(w) -> bool:
    row = _the_appointment(w)
    return any(str(r.get("appointment_id")) == str(row.id) and r.get("stage") == "unknown_outcome"
               for r in w.unresolved)


@pytest.mark.parametrize("shape", ALL_SHAPES)
@pytest.mark.parametrize("business", sorted(BUSINESSES))
async def test_realtime_voice_move_is_proved_by_googles_reply(monkeypatch, business, shape):
    w = _google_world(monkeypatch, business, shape)
    p = await _realtime_booked(w)
    row = _the_appointment(w)
    original_start = row.start_time
    event_id = row.external_booking_id
    assert event_id and row.booking_provider == BookingProvider.GOOGLE_CALENDAR.value

    await p.says("Can we move it to Thursday at 3 PM instead?")
    staged = await p.propose(operation="reschedule", **THREE(w))
    assert staged["status"] == "draft", staged
    assert w.google.patches == [], "Google was written before the caller said yes"
    heard_before, outputs_before = len(p.heard), len(p.outputs)
    await p.says("Yes, please.")
    about_the_move = p.heard[heard_before:] + [str(o) for o in p.outputs[outputs_before:]]

    assert len(w.google.patches) == 1 and w.google.patches[0]["eventId"] == event_id
    if shape == "valid":
        assert row.start_time != original_start
        assert w.google.start_of(event_id) == w.google.patches[0]["body"]["start"]["dateTime"]
        assert w.unresolved == []
        return

    assert row.start_time == original_start, "the booking followed a move Google did not prove"
    assert _unknown_outcome_recorded(w), "no durable unknown-outcome record"
    # What the caller heard AFTER the move attempt (the earlier booking confirmation
    # legitimately says "all set") must not claim the move, and must say it is unconfirmed.
    said = " ".join(about_the_move).lower()
    assert "moved it" not in said and "all set" not in said and "rescheduled" not in said, said
    assert "didn't confirm" in said or "did not confirm" in said, said

    # An immediate second attempt, by whatever route, must not reach Google again.
    await p.says("Yes, please.")
    await p.says("Can we move it to Thursday at 3 PM instead?")
    await p.propose(operation="reschedule", **THREE(w))
    await p.says("Yes, please.")
    assert len(w.google.patches) == 1, "an unsafe retry reached Google again"
    assert row.start_time == original_start


@pytest.mark.parametrize("shape", ALL_SHAPES)
@pytest.mark.parametrize("business", sorted(BUSINESSES))
async def test_legacy_voice_move_is_proved_by_googles_reply(monkeypatch, business, shape):
    w = _google_world(monkeypatch, business, shape)
    c = await _legacy_booked_google(w)
    row = _the_appointment(w)
    original_start = row.start_time
    event_id = row.external_booking_id
    assert event_id and row.booking_provider == BookingProvider.GOOGLE_CALENDAR.value

    await c.says("Can you move it to 3 instead?", intent=_intent("reschedule", T3, w.first),
                 reply="3 PM is available. Would you like me to move it?")
    assert w.google.patches == [], "Google was written before the caller said yes"
    said = await c.says("Yes.", reply="Done.")

    assert len(w.google.patches) == 1 and w.google.patches[0]["eventId"] == event_id
    if shape == "valid":
        assert row.start_time != original_start
        assert w.google.start_of(event_id) == w.google.patches[0]["body"]["start"]["dateTime"]
        assert w.unresolved == []
        return

    assert row.start_time == original_start, "the booking followed a move Google did not prove"
    assert _unknown_outcome_recorded(w), "no durable unknown-outcome record"
    lowered = said.lower()
    assert "moved it" not in lowered and "all set" not in lowered and "rescheduled" not in lowered, said

    await c.says("Yes.", reply="Done.")
    await c.says("Can you move it to 3 instead?", intent=_intent("reschedule", T3, w.first),
                 reply="3 PM is available. Would you like me to move it?")
    await c.says("Yes.", reply="Done.")
    assert len(w.google.patches) == 1, "an unsafe retry reached Google again"
    assert row.start_time == original_start
