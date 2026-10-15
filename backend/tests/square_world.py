"""A stateful in-memory Square for booking-engine regression tests.

Not a test module (no ``test_`` prefix). It stands in for the Square Bookings
API behind the REAL ``SpaBookingAdapter`` -> ``SquareAdapter`` code path, and it
remembers what was written, so a test can assert on what Square actually holds
after a call, not just on what CARA said:

* ``POST /v2/bookings`` stores a booking (id, version, start_at, segments).
* ``GET /v2/bookings/{id}`` returns the stored booking.
* ``PUT /v2/bookings/{id}`` applies ``start_at`` / ``appointment_segments``
  with optimistic concurrency on ``version`` and replays an idempotency key.
* ``POST /v2/bookings/availability/search`` returns configured openings,
  filtered by service variation, team member and range, and hides any team
  member who already holds an overlapping booking (except the booking named
  by a reschedule search's ``booking_id``).

Knobs simulate provider faults: ``ignore_segment_updates`` (Square answers the
PUT but keeps the old service) and ``wrong_start_on_update`` (Square stores a
different time than requested).
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.models import Appointment, AppointmentStatus, BookingProvider
from app.services import appointment_booking_service as svc
from app.services.booking_adapters import SpaBookingAdapter
from app.services.booking_adapters.base import BookingProviderError
from app.services.call_state import CallSession
from tests.conftest import make_spa

LOCATION = "loc_123"
CHICAGO = "America/Chicago"

# Thursday 22 October 2026, 2pm Chicago (CDT, UTC-5) == 19:00 UTC.
THU_2PM_UTC = datetime(2026, 10, 22, 19, 0, tzinfo=timezone.utc)

MENU = [
    {"name": "Swedish Massage", "duration_minutes": 60,
     "square_variation_id": "var_swedish_60", "square_variation_version": 3},
    {"name": "Signature Facial", "duration_minutes": 60,
     "square_variation_id": "var_facial_60", "square_variation_version": 5},
    {"name": "Hot Stone Massage", "duration_minutes": 90,
     "square_variation_id": "var_hotstone_90", "square_variation_version": 2},
]
# Square catalog: everything on the default dashboard menu plus variations a
# test can put on its own menu.
CATALOG = MENU + [
    {"name": "Swedish Massage 60", "duration_minutes": 60,
     "square_variation_id": "var_swedish_60", "square_variation_version": 3},
    {"name": "Swedish Massage 90", "duration_minutes": 90,
     "square_variation_id": "var_swedish_90", "square_variation_version": 3},
]
VERSIONS = {item["square_variation_id"]: item["square_variation_version"] for item in CATALOG}
MINUTES = {item["square_variation_id"]: item["duration_minutes"] for item in CATALOG}


def _z(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class FakeSquare:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.bookings: dict[str, dict[str, Any]] = {}
        self.openings: list[dict[str, Any]] = []   # {start, team, variation}
        self._idempotent: dict[str, dict[str, Any]] = {}
        self._next = 0
        self.ignore_segment_updates = False
        self.wrong_start_on_update = False

    # -- configuration -------------------------------------------------- #
    def open(self, start: datetime, variation: str, team: str = "TM_ANA") -> None:
        self.openings.append({"start": start.astimezone(timezone.utc), "team": team, "variation": variation})

    def open_all(self, start: datetime, team: str = "TM_ANA") -> None:
        for variation in VERSIONS:
            self.open(start, variation, team)

    # -- inspection ----------------------------------------------------- #
    def writes(self, method: str, prefix: str = "/v2/bookings") -> list[tuple[str, dict]]:
        return [
            (path, body) for m, path, body in self.calls
            if m == method and path.startswith(prefix) and "availability" not in path
        ]

    @property
    def creates(self) -> list[dict]:
        return [body for path, body in self.writes("POST") if path == "/v2/bookings"]

    @property
    def updates(self) -> list[tuple[str, dict]]:
        return self.writes("PUT")

    def active(self) -> list[dict[str, Any]]:
        return [b for b in self.bookings.values() if b["status"] == "ACCEPTED"]

    # -- transport ------------------------------------------------------ #
    async def __call__(self, method: str, path: str, *, json: dict | None = None) -> dict:
        body = copy.deepcopy(json or {})
        self.calls.append((method, path, body))
        if path.startswith("/v2/locations/"):
            return {"location": {"id": LOCATION, "name": "Test", "timezone": CHICAGO, "status": "ACTIVE"}}
        if path == "/v2/catalog/search-catalog-items":
            return {"items": []}
        if path == "/v2/team-members/search":
            return {"team_members": [
                {"id": "TM_ANA", "given_name": "Ana", "family_name": "Lopez", "status": "ACTIVE", "is_owner": False},
                {"id": "TM_BEA", "given_name": "Bea", "family_name": "Kim", "status": "ACTIVE", "is_owner": False},
            ]}
        if path == "/v2/customers/search":
            return {"customers": [{"id": "cust_1", "given_name": "Ada", "phone_number": "+15550000002"}]}
        if path == "/v2/customers":
            return {"customer": {"id": "cust_1"}}
        if path == "/v2/bookings/availability/search":
            return {"availabilities": self._search(body.get("query", {}).get("filter", {}))}
        if path == "/v2/bookings" and method == "POST":
            return {"booking": self._create(body)}
        if path.startswith("/v2/bookings/") and method == "GET":
            booking = self.bookings.get(path.rsplit("/", 1)[-1])
            if booking is None:
                raise BookingProviderError("not found", code="NOT_FOUND")
            return {"booking": copy.deepcopy(booking)}
        if path.startswith("/v2/bookings/") and method == "PUT":
            return {"booking": self._update(path.rsplit("/", 1)[-1], body)}
        if path.startswith("/v2/bookings/") and path.endswith("/cancel") and method == "POST":
            return {"booking": self._cancel(path.split("/")[-2], body)}
        raise AssertionError(f"unexpected Square call {method} {path}")

    # -- behaviour ------------------------------------------------------ #
    def _busy(self, team: str, start: datetime, minutes: int, ignore: str | None) -> bool:
        end = start + timedelta(minutes=minutes)
        for booking in self.active():
            if booking["id"] == ignore:
                continue
            b_start = _parse(booking["start_at"])
            for seg in booking["appointment_segments"]:
                b_end = b_start + timedelta(minutes=int(seg["duration_minutes"]))
                if seg["team_member_id"] == team and b_start < end and b_end > start:
                    return True
        return False

    def _search(self, flt: dict) -> list[dict]:
        rng = flt.get("start_at_range") or {}
        lo, hi = _parse(rng["start_at"]), _parse(rng["end_at"])
        ignore = flt.get("booking_id")
        if ignore:
            held = self.bookings[ignore]["appointment_segments"][0]
            wanted, teams = held["service_variation_id"], [held["team_member_id"]]
        else:
            seg = (flt.get("segment_filters") or [{}])[0]
            wanted = seg.get("service_variation_id")
            teams = ((seg.get("team_member_id_filter") or {}).get("any")) or None
        out = []
        for slot in self.openings:
            if not (lo <= slot["start"] < hi) or slot["variation"] != wanted:
                continue
            if teams and slot["team"] not in teams:
                continue
            if self._busy(slot["team"], slot["start"], MINUTES[wanted], ignore):
                continue
            out.append({
                "start_at": _z(slot["start"]),
                "location_id": LOCATION,
                "appointment_segments": [{
                    "team_member_id": slot["team"],
                    "service_variation_id": wanted,
                    "service_variation_version": VERSIONS[wanted],
                    "duration_minutes": MINUTES[wanted],
                }],
            })
        return out

    def _create(self, body: dict) -> dict:
        key = body.get("idempotency_key")
        if key in self._idempotent:
            return copy.deepcopy(self._idempotent[key])
        self._next += 1
        booking = dict(body["booking"])
        booking.update(id=f"sq_{self._next}", version=1, status="ACCEPTED")
        for seg in booking["appointment_segments"]:
            seg.setdefault("duration_minutes", MINUTES[seg["service_variation_id"]])
        self.bookings[booking["id"]] = booking
        self._idempotent[key] = copy.deepcopy(booking)
        return copy.deepcopy(booking)

    def _cancel(self, booking_id: str, body: dict) -> dict:
        """CancelBooking: version-checked, idempotent, like Square."""
        key = body.get("idempotency_key")
        if key in self._idempotent:
            return copy.deepcopy(self._idempotent[key])
        stored = self.bookings[booking_id]
        if body.get("booking_version") != stored["version"]:
            raise BookingProviderError("version mismatch", code="CONFLICT")
        stored["status"] = "CANCELLED_BY_SELLER"
        stored["version"] += 1
        self._idempotent[key] = copy.deepcopy(stored)
        return copy.deepcopy(stored)

    def _update(self, booking_id: str, body: dict) -> dict:
        key = body.get("idempotency_key")
        if key in self._idempotent:
            return copy.deepcopy(self._idempotent[key])
        stored = self.bookings[booking_id]
        change = body["booking"]
        if change.get("version") != stored["version"]:
            raise BookingProviderError("version mismatch", code="CONFLICT")
        if "start_at" in change:
            stored["start_at"] = change["start_at"]
            if self.wrong_start_on_update:
                stored["start_at"] = _z(_parse(change["start_at"]) + timedelta(minutes=30))
        if "appointment_segments" in change and not self.ignore_segment_updates:
            segments = copy.deepcopy(change["appointment_segments"])
            for seg in segments:
                seg.setdefault("duration_minutes", MINUTES[seg["service_variation_id"]])
            stored["appointment_segments"] = segments
        stored["version"] += 1
        self._idempotent[key] = copy.deepcopy(stored)
        return copy.deepcopy(stored)


class Store:
    def __init__(self) -> None:
        self.rows: list[Appointment] = []

    @property
    def live(self) -> list[Appointment]:
        return [r for r in self.rows if r.status in (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)]


class FakeDB:
    """Rows become durable on commit; rollback discards uncommitted rows."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._pending: list[Appointment] = []

    def add(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if isinstance(obj, Appointment):
            self._pending.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        for obj in self._pending:
            if obj not in self.store.rows:
                self.store.rows.append(obj)
        self._pending = []

    async def refresh(self, _obj) -> None:
        return None

    async def rollback(self) -> None:
        self._pending = []

    async def get(self, *_a, **_k):
        return None


async def _value(v):
    return v


def square_spa(services=None, **overrides):
    return make_spa(
        booking_provider=BookingProvider.SQUARE,
        booking_config={"access_token": "token", "location_id": LOCATION},
        business_hours={day: [{"open": "09:00", "close": "19:00"}]
                        for day in ("mon", "tue", "wed", "thu", "fri", "sat")},
        timezone=CHICAGO,
        services=[dict(item) for item in (services or MENU)],
        **overrides,
    )


def wire(monkeypatch, spa=None, adapter=None):
    """Point the real booking service at in-memory rows and a fake Square."""
    spa = spa or square_spa()
    square = FakeSquare()
    adapter = adapter or SpaBookingAdapter(spa)
    if getattr(adapter, "delegate", None) is not None and adapter.delegate.provider == "square":
        monkeypatch.setattr(adapter.delegate, "_request", square)
    store = Store()
    db = FakeDB(store)
    contact = SimpleNamespace(id=uuid.uuid4(), phone="+15550000002", email=None)

    async def _noop(*_a, **_k):
        return None

    async def _by_key(_db, _scope, key):
        return next((r for r in store.live if getattr(r, "booking_intent_key", None) == key), None)

    async def _by_id(_db, _scope, appointment_id):
        if not appointment_id:
            return None
        return next((r for r in store.live if str(r.id) == str(appointment_id)), None)

    async def _conflict(_db, _scope, start, end, capacity=1, exclude_id=None, exclude_intent_key=None):
        return False  # Square is the source of truth for these tests.

    async def _card_status(**_k):
        from app.models.appointment import CardStatus
        return CardStatus.NOT_REQUIRED

    monkeypatch.setattr(svc, "_load_spa", lambda *_a, **_k: _value(spa))
    monkeypatch.setattr(svc, "get_booking_adapter", lambda **_k: adapter)
    monkeypatch.setattr(svc, "_lock_call", _noop)
    monkeypatch.setattr(svc, "_lock_slot", _noop)
    monkeypatch.setattr(svc, "_unresolved_booking", _noop)
    monkeypatch.setattr(svc, "_load_call_log_id", _noop)
    monkeypatch.setattr(svc, "persist_caller_identity", _noop)
    monkeypatch.setattr(svc, "_notify_staff_event", _noop)
    monkeypatch.setattr(svc, "_notify_provider_error", _noop)
    monkeypatch.setattr(svc, "upsert_external_customer_link", _noop)
    monkeypatch.setattr(svc, "card_status_for_booked_appointment", _card_status)
    monkeypatch.setattr(svc, "_resolve_contact", lambda *_a, **_k: _value(contact))
    monkeypatch.setattr(svc, "_load_by_intent_key", _by_key)
    monkeypatch.setattr(svc, "_load_active_appointment", _by_id)
    monkeypatch.setattr(svc, "_load_call_owned_appointment", _noop)
    async def _contact_for(_db, _scope, session):
        # The caller's own number finds their contact, as the real lookup does.
        return contact if getattr(session, "from_number", None) == contact.phone else None

    monkeypatch.setattr(svc, "_find_contact_by_phone", _contact_for)
    monkeypatch.setattr(svc, "_has_conflict", _conflict)
    return SimpleNamespace(spa=spa, square=square, adapter=adapter, store=store, db=db, contact=contact)


def call(spa, sid: str = "CA-p0") -> CallSession:
    return CallSession(sid, "inbound", "+15550000002", "+15550000001",
                       tenant_id=str(spa.id), timezone=CHICAGO)


def local(dt: datetime) -> str:
    """Business-local wall clock, as the voice tools send it."""
    from zoneinfo import ZoneInfo
    return dt.astimezone(ZoneInfo(CHICAGO)).replace(tzinfo=None).isoformat(timespec="seconds")


def voice_for(world, session, monkeypatch):
    """A realtime voice session wired to this world's DB and appointment lookup."""
    from contextlib import asynccontextmanager

    from app.services import xai_realtime
    from app.services.xai_realtime import XAIVoiceSession

    @asynccontextmanager
    async def _db():
        yield world.db

    async def _upcoming(_db, _session, limit=10):
        return sorted(world.store.live, key=lambda row: row.start_time)[:limit]

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(xai_realtime, "AsyncSessionLocal", _db)
    monkeypatch.setattr(xai_realtime, "lookup_upcoming_appointments", _upcoming)
    voice = XAIVoiceSession(session.call_sid, session)
    voice._send = _noop
    voice._cancel_active_response = _noop
    voice._persist_session = _noop
    voice._arm_availability_hold = _noop
    return voice
