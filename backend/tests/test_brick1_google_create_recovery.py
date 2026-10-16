"""G1: a Google create whose reply is lost or unclear is resolved by ONE read
of the exact deterministic event id, and accepted only on full proof.

SIMULATED Google transport only (``google_calendar._service`` is replaced);
the service, adapter, booking engine, realtime dispatcher and legacy handler
are the real code. Nothing here talks to Google.

Replaces, as stricter tests, Claude's original reproduction
``test_claude_repro_google_lost_create.py`` (kept as evidence; its fake had no
``events.get`` and dropped private properties, so it cannot exercise a lookup):

* "lost reply + verified event => exactly ONE Google event, ONE local
  appointment whose external_booking_id is that event id"
      -> test_service_lost_reply_is_recovered_*,
         test_voice_lost_reply_books_once_with_the_recovered_event
* "never a second insert under a different id"
      -> every test asserts ``inserted_ids`` holds one id at most / no new id
* "lookup unavailable or failing => UNKNOWN, never success, never failed"
      -> test_unverifiable_recovery_is_unknown[...],
         test_voice_unverified_recovery_is_unknown_not_booked
"""
from __future__ import annotations

import copy
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httplib2
import pytest
from googleapiclient.errors import HttpError

from app.services import booking_escalations
from app.services import google_calendar as gc
from app.services.booking_adapters.base import BookingProviderError
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
from app.services.google_calendar import GoogleCalendarError
from tests import square_world
from tests.square_world import local, square_spa, wire
from tests.test_brick1_booking_change_guard import BUSINESSES, LegacyCall, Phone, T2, _intent

CALENDAR = "simulated-calendar"
KEY = "CA0123456789abcdef0123456789abcdef:01234567-89ab-cdef-0123-456789abcdef"
START, END = T2, T2 + timedelta(hours=1)


def _http(code: int) -> HttpError:
    return HttpError(httplib2.Response({"status": code}), b'{"error":{"code":%d}}' % code)


class Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class RecoveringGoogle:
    """Holds the calendar's TRUE state and behaves like Google for insert/get.

    ``first_insert``: "ok" | "lost_reply" (store, then the reply is lost) |
    "http_500_stored" | "http_503_not_stored" | "minimal_reply" (store, reply
    without fields) | "contradictory_reply" (store, reply with other times) |
    "http_400" | "http_403". A same-id insert later answers 409, like Google.
    ``tamper`` changes the stored event before a recovery GET; ``get_fails``
    makes the GET raise.
    """

    def __init__(self, first_insert="ok", tamper=None, get_fails=None):
        self.first_insert = first_insert
        self.tamper = tamper
        self.get_fails = get_fails
        self.state: dict[str, dict] = {}
        self.inserted_ids: list[str] = []
        self.gets: list[str] = []

    def service(self):
        return SimpleNamespace(events=lambda: SimpleNamespace(
            insert=lambda **kw: Req(lambda: self._insert(kw)),
            get=lambda **kw: Req(lambda: self._get(kw)),
            patch=lambda **kw: Req(lambda: {}),
            delete=lambda **kw: Req(lambda: {}),
        ), freebusy=lambda: SimpleNamespace(
            query=lambda body: Req(lambda: {"calendars": {CALENDAR: {"busy": []}}})))

    def _insert(self, kw):
        body = copy.deepcopy(kw["body"])
        event_id = body.get("id") or f"srv{len(self.state) + 1}"
        self.inserted_ids.append(event_id)
        if event_id in self.state:
            raise _http(409)
        mode = self.first_insert if len(self.inserted_ids) == 1 else "ok"
        if mode == "http_400":
            raise _http(400)
        if mode == "http_403":
            raise _http(403)
        if mode == "http_503_not_stored":
            raise _http(503)
        event = {"id": event_id, "status": "confirmed", "start": body["start"], "end": body["end"],
                 "summary": body.get("summary")}
        if body.get("extendedProperties"):
            event["extendedProperties"] = copy.deepcopy(body["extendedProperties"])
        self.state[event_id] = event
        if mode == "lost_reply":
            raise TimeoutError("simulated: the event was stored, the reply was lost")
        if mode == "http_500_stored":
            raise _http(500)
        if mode == "minimal_reply":
            return {"id": event_id}
        if mode == "contradictory_reply":
            reply = copy.deepcopy(event)
            reply["start"] = {"dateTime": "2031-01-01T09:00:00+00:00", "timeZone": "UTC"}
            return reply
        return copy.deepcopy(event)

    def _get(self, kw):
        self.gets.append(kw["eventId"])
        if self.get_fails == "transport":
            raise ConnectionError("simulated lookup failure")
        if self.tamper and kw["eventId"] in self.state:
            self.tamper(self.state[kw["eventId"]])
        if kw["eventId"] not in self.state:
            raise _http(404)
        return copy.deepcopy(self.state[kw["eventId"]])


def _install(monkeypatch, fake, tenant=None):
    connection = SimpleNamespace(selected_calendar_id=CALENDAR, spa_id=tenant or uuid.uuid4())

    async def _service(conn):
        assert conn is connection
        return fake.service()

    monkeypatch.setattr(gc, "_service", _service)
    return connection


async def _create(connection, key=KEY):
    return await gc.create_event(connection, title="Service visit", start=START, end=END,
                                 timezone_name="America/Chicago", description="", event_id=key)


# --------------------------------------------------------------------------- #
# Service level
# --------------------------------------------------------------------------- #
async def test_normal_create_carries_a_verifying_tag_and_needs_no_lookup(monkeypatch):
    fake = RecoveringGoogle("ok")
    conn = _install(monkeypatch, fake)
    event_id = await _create(conn)
    private = fake.state[event_id]["extendedProperties"]["private"]
    assert private["cara_v"] == "1" and private["cara_tenant"] == str(conn.spa_id)
    assert len(private["cara_tag"]) == 64
    assert fake.gets == [] and fake.inserted_ids == [event_id]


@pytest.mark.parametrize("mode", ["lost_reply", "http_500_stored", "minimal_reply", "contradictory_reply"])
async def test_service_lost_reply_is_recovered_by_one_exact_lookup(monkeypatch, mode):
    fake = RecoveringGoogle(mode)
    conn = _install(monkeypatch, fake)
    event_id = await _create(conn)
    assert list(fake.state) == [event_id]
    assert fake.gets == [event_id]                 # exactly one read, of the exact id
    assert fake.inserted_ids == [event_id]         # never a second insert


async def test_service_retry_after_lost_reply_gets_409_and_recovers_the_same_event(monkeypatch):
    fake = RecoveringGoogle("ok")
    conn = _install(monkeypatch, fake)
    first = await _create(conn)                    # e.g. the earlier attempt whose reply was lost
    again = await _create(conn)                    # same key => same id => 409 => verified read
    assert again == first and list(fake.state) == [first]
    assert fake.inserted_ids == [first, first]     # same id both times, never a new one


def _shift_start(event):
    event["start"] = {"dateTime": "2031-01-01T09:00:00+00:00", "timeZone": "UTC"}


def _cancel(event):
    event["status"] = "cancelled"


def _drop_tag(event):
    event.pop("extendedProperties", None)


def _other_tenant(event):
    event["extendedProperties"]["private"]["cara_tenant"] = str(uuid.uuid4())


def _forge_tag(event):
    event["extendedProperties"]["private"]["cara_tag"] = "0" * 64


def _unknown_version(event):
    event["extendedProperties"]["private"]["cara_v"] = "2"


@pytest.mark.parametrize("name,tamper,get_fails,first", [
    ("different_time", _shift_start, None, "lost_reply"),
    ("cancelled_event", _cancel, None, "lost_reply"),
    ("missing_tag", _drop_tag, None, "lost_reply"),
    ("other_business_tag", _other_tenant, None, "lost_reply"),
    ("forged_tag", _forge_tag, None, "lost_reply"),
    ("unknown_tag_version", _unknown_version, None, "lost_reply"),
    ("lookup_transport_failure", None, "transport", "lost_reply"),
    ("not_found_404", None, None, "http_503_not_stored"),
])
async def test_unverifiable_recovery_is_unknown(monkeypatch, name, tamper, get_fails, first):
    fake = RecoveringGoogle(first, tamper=tamper, get_fails=get_fails)
    conn = _install(monkeypatch, fake)
    with pytest.raises(GoogleCalendarError) as exc:
        await _create(conn)
    assert exc.value.status == "unverified_response", name
    assert len(set(fake.inserted_ids)) <= 1        # no new id, no second create
    assert len(fake.inserted_ids) == 1


async def test_copied_signed_event_under_another_id_cannot_pass(monkeypatch):
    """A whole signed event copied by a calendar editor sits under another id;
    recovery reads ONLY the expected id, which does not exist => unknown."""
    fake = RecoveringGoogle("ok")
    conn = _install(monkeypatch, fake)
    original = await _create(conn)
    copy_id = "copy" + original[4:]
    fake.state[copy_id] = dict(copy.deepcopy(fake.state.pop(original)), id=copy_id)
    fake.first_insert = "http_503_not_stored"
    fake.inserted_ids.clear()
    with pytest.raises(GoogleCalendarError) as exc:
        await _create(conn)
    assert exc.value.status == "unverified_response"
    assert fake.gets == [original]


async def test_rotated_secret_key_makes_old_tags_unverifiable(monkeypatch):
    fake = RecoveringGoogle("ok")
    conn = _install(monkeypatch, fake)
    await _create(conn)
    monkeypatch.setattr(gc.settings, "SECRET_KEY", "rotated-" + "k" * 40)
    with pytest.raises(GoogleCalendarError) as exc:
        await _create(conn)                        # 409 => tag no longer verifies
    assert exc.value.status == "unverified_response"


@pytest.mark.parametrize("mode,status", [("http_400", "google_api_error"),
                                         ("http_403", "insufficient_permissions")])
async def test_definite_refusals_are_not_recovered(monkeypatch, mode, status):
    fake = RecoveringGoogle(mode)
    conn = _install(monkeypatch, fake)
    with pytest.raises(GoogleCalendarError) as exc:
        await _create(conn)
    assert exc.value.status == status
    assert fake.gets == []


async def test_without_a_booking_reference_nothing_is_guessed(monkeypatch):
    fake = RecoveringGoogle("lost_reply")
    conn = _install(monkeypatch, fake)
    with pytest.raises(GoogleCalendarError) as exc:
        await _create(conn, key=None)
    assert exc.value.status == "unverified_response"
    assert fake.gets == []


async def test_adapter_maps_unverified_create_to_unknown_outcome(monkeypatch):
    fake = RecoveringGoogle("lost_reply", tamper=_cancel)
    conn = _install(monkeypatch, fake)
    adapter = GoogleCalendarAdapter(calendar_id=CALENDAR, connection=conn)
    ctx = SimpleNamespace(start=START, end=END, service_description="Service visit", title="Visit",
                          customer_name="Ada Lovelace", customer_phone="+15550000002",
                          customer_email=None, notes=None, booking_reference=KEY)
    with pytest.raises(BookingProviderError) as exc:
        await adapter.create_booking(ctx)
    assert exc.value.code == "BOOKING_OUTCOME_UNKNOWN"
    assert exc.value.retryable is False


# --------------------------------------------------------------------------- #
# Voice paths, several unrelated businesses
# --------------------------------------------------------------------------- #
def _voice_world(monkeypatch, business, fake):
    menu = BUSINESSES[business]
    for item in menu:
        monkeypatch.setitem(square_world.VERSIONS, item["square_variation_id"], item["square_variation_version"])
        monkeypatch.setitem(square_world.MINUTES, item["square_variation_id"], item["duration_minutes"])
    spa = square_spa(services=menu)
    conn = _install(monkeypatch, fake, tenant=spa.id)
    adapter = GoogleCalendarAdapter(calendar_id=CALENDAR, connection=conn)
    w = wire(monkeypatch, spa=spa, adapter=adapter)
    w.google, w.monkeypatch = fake, monkeypatch
    w.first = menu[0]["name"]
    w.escalations = []

    async def _record(_db, _spa, call_sid, details, exc):
        w.escalations.append({"call_sid": call_sid, "code": getattr(exc, "code", None), **details})
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(booking_escalations, "record", _record)
    return w


async def _book(w, path):
    if path == "realtime":
        p = Phone(w)
        p.voice._pending_caller = f"I'd like a {w.first} on Thursday at 2 PM. My name is Ada Lovelace."
        p.voice._flush_caller_turn()
        out = await p.propose(requested_start_iso=local(T2), service_description=w.first,
                              caller_name="Ada Lovelace", operation="schedule")
        assert out["status"] == "draft", out
        await p.says("Yes, please.")
        return p.heard
    c = LegacyCall(w, w.monkeypatch)
    await c.says(f"I'd like a {w.first} Thursday at 2 PM, I'm Ada Lovelace.",
                 intent=_intent("schedule", T2, w.first),
                 reply="Thursday at 2 PM is open. Would you like me to book that?")
    await c.says("Yes.", reply="You're all set. Is there anything else I can help you with today?")
    return []


@pytest.mark.parametrize("path", ["realtime", "legacy"])
@pytest.mark.parametrize("business", sorted(BUSINESSES))
async def test_voice_lost_reply_books_once_with_the_recovered_event(monkeypatch, business, path):
    w = _voice_world(monkeypatch, business, RecoveringGoogle("lost_reply"))
    await _book(w, path)
    assert len(w.google.state) == 1                       # ONE Google event
    rows = list(w.store.live)
    assert len(rows) == 1                                 # ONE local appointment
    assert rows[0].external_booking_id in w.google.state  # linked to that event
    assert len(set(w.google.inserted_ids)) == 1           # never a different id
    assert w.escalations == []


@pytest.mark.parametrize("path", ["realtime", "legacy"])
@pytest.mark.parametrize("business", sorted(BUSINESSES))
async def test_voice_unverified_recovery_is_unknown_not_booked(monkeypatch, business, path):
    w = _voice_world(monkeypatch, business, RecoveringGoogle("lost_reply", tamper=_shift_start))
    heard = await _book(w, path)
    assert list(w.store.live) == []                       # nothing recorded as booked
    assert len(w.escalations) == 1                        # durable staff work
    assert w.escalations[0]["code"] == "BOOKING_OUTCOME_UNKNOWN"
    assert not any("you're all set" in line.casefold() for line in heard)
    assert len(w.google.inserted_ids) == 1                # no blind re-insert
