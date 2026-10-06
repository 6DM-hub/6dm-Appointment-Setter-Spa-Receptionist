"""The booking state machine: one appointment per intent, whatever the caller does.

The bug these cover: the agent used to write an appointment the moment it heard
a date, so "September 20... actually, make it the 22nd" left two appointments
behind and the final confirmation collided with the first one and told the
caller their own slot was taken.

Each test drives the real `stage_booking` / `confirm_booking` / `cancel_booking`
functions against an in-memory appointment store and a recording calendar
adapter, then asserts on BOTH sides of the write: how many rows exist, and how
many events the calendar was actually asked to create. A fix that dedupes rows
but still creates two calendar events would fail here.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import Appointment, AppointmentStatus
from app.services import appointment_booking_service as svc
from app.services.appointment_booking_service import (
    BookingOutcome,
    cancel_booking,
    confirm_booking,
    stage_booking,
)
from app.services.booking_adapters.base import (
    AvailabilityVerdict,
    BookingProviderError,
    ExternalBooking,
)
from app.services.booking_state import (
    arm_verified_proposal,
    get_draft,
    is_affirmative,
    start_new_intent,
    save_draft,
    utterance_modifies_booking,
)
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests.conftest import make_spa

SEPT_20 = datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc)
SEPT_22 = datetime(2026, 9, 22, 14, 0, tzinfo=timezone.utc)
SEPT_24 = datetime(2026, 9, 24, 14, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
class _RecordingAdapter:
    """A calendar that remembers every write it was asked to perform."""

    provider = "internal"
    calendar_label = "the internal calendar"
    default_title = "Appointment"
    default_duration_minutes = 60

    def __init__(self) -> None:
        self.created: list[tuple[datetime, datetime]] = []
        self.created_contexts: list = []
        self.moved: list[tuple[str | None, datetime, datetime]] = []
        self.cancelled: list[str | None] = []
        self.busy: list[tuple[datetime, datetime]] = []
        self._next = 0
        # Test hooks for Phase 1 confirmation-gating scenarios: None (default)
        # behaves as a normal successful provider; "no_id" simulates a
        # provider that reports success but returns no booking id; an
        # Exception instance is raised instead of creating anything.
        self.create_booking_failure: Exception | str | None = None

    async def booking_timezone_name(self) -> str | None:
        return None

    async def list_openings(self, ctx, range_start, range_end):
        return []

    async def check_availability(self, ctx) -> AvailabilityVerdict:
        for start, end in self.busy:
            if ctx.start < end and ctx.end > start:
                return AvailabilityVerdict.no("That time is already occupied.")
        service = (ctx.service_description or "").lower()
        variation = "var_swedish" if "swedish" in service else "var_hydrolux" if "hydrolux" in service else "var_internal"
        return AvailabilityVerdict.ok(slot={
            "start": ctx.start.isoformat(),
            "duration_minutes": int((ctx.end - ctx.start).total_seconds() / 60),
            "service_variation_id": variation,
            "service_variation_version": 1,
            "team_member_id": "TM_1",
            "location_id": "loc_1",
        })

    async def create_booking(self, ctx) -> ExternalBooking:
        if isinstance(self.create_booking_failure, Exception):
            raise self.create_booking_failure
        self._next += 1
        self.created.append((ctx.start, ctx.end))
        self.created_contexts.append(ctx)
        if self.create_booking_failure == "no_id":
            return ExternalBooking(provider=self.provider, external_id=None)
        return ExternalBooking(provider=self.provider, external_id=f"evt-{self._next}")

    async def move_booking(self, booking, start, end) -> ExternalBooking:
        self.moved.append((booking.external_id, start, end))
        return booking

    async def cancel_booking(self, booking) -> None:
        self.cancelled.append(booking.external_id)


class _Store:
    """Stands in for the appointments table."""

    def __init__(self) -> None:
        self.rows: list[Appointment] = []

    @property
    def live(self) -> list[Appointment]:
        return [
            r for r in self.rows
            if r.status in (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)
        ]


class _FakeDB:
    """Mirrors the transaction boundary a real `AsyncSession` enforces: a row
    added via `add()`/`flush()` only becomes durable on `commit()`, and
    `rollback()` discards it — matching how a real Postgres session would
    undo an uncommitted INSERT. Without this, the Phase 1 confirmation-gating
    tests (provider failure after the row was flushed but before it was
    committed) would see a "booked" row that a real database never kept."""

    def __init__(self, store: _Store) -> None:
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
        return None


@pytest.fixture
def world(monkeypatch):
    """Wire the booking service to the in-memory store and recording calendar."""
    store = _Store()
    adapter = _RecordingAdapter()
    db = _FakeDB(store)
    spa = make_spa()
    contact_id = uuid.uuid4()

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(svc, "_load_spa", lambda *_a, **_k: _value(spa))
    monkeypatch.setattr(svc, "get_booking_adapter", lambda **_k: adapter)
    monkeypatch.setattr(svc, "_lock_call", _noop)
    monkeypatch.setattr(svc, "_lock_slot", _noop)
    monkeypatch.setattr(svc, "_load_call_log_id", _noop)
    monkeypatch.setattr(svc, "persist_caller_identity", _noop)

    class _Contact:
        id = contact_id

    monkeypatch.setattr(svc, "_resolve_contact", lambda *_a, **_k: _value(_Contact()))

    async def _by_key(_db, _scope, key):
        return next(
            (r for r in store.live if getattr(r, "booking_intent_key", None) == key),
            None,
        )

    async def _by_id(_db, _scope, appointment_id):
        if not appointment_id:
            return None
        return next((r for r in store.live if str(r.id) == str(appointment_id)), None)

    async def _by_call(_db, _scope, _call_sid):
        return None

    async def _upcoming(_db, _scope, cid):
        return next((r for r in store.live if r.contact_id == cid), None)

    async def _conflict(
        _db, _scope, start, end, capacity=1, exclude_id=None, exclude_intent_key=None
    ):
        overlapping = [
            r for r in store.live
            if r.start_time < end and r.end_time > start
            and (exclude_id is None or r.id != exclude_id)
            and (
                exclude_intent_key is None
                or getattr(r, "booking_intent_key", None) != exclude_intent_key
            )
        ]
        return len(overlapping) >= max(capacity, 1)

    monkeypatch.setattr(svc, "_load_by_intent_key", _by_key)
    monkeypatch.setattr(svc, "_load_active_appointment", _by_id)
    monkeypatch.setattr(svc, "_load_call_owned_appointment", _by_call)
    monkeypatch.setattr(svc, "_find_upcoming_appointment", _upcoming)
    monkeypatch.setattr(svc, "_find_upcoming_appointments", lambda *_a, **_k: _value([]))
    monkeypatch.setattr(svc, "_find_contact_by_phone", lambda *_a, **_k: _value(None))
    monkeypatch.setattr(svc, "_has_conflict", _conflict)

    session = CallSession(
        "CA-scenario", "inbound", "+15550000002", "+15550000001",
        tenant_id=str(spa.id),
    )
    return {
        "db": db, "store": store, "adapter": adapter,
        "session": session, "spa": spa, "contact_id": contact_id,
    }


async def _value(v):
    return v


def _wants(start: datetime, service: str = "60 minute deep tissue massage"):
    return AppointmentIntent(
        intent="schedule",
        confidence=1.0,
        requested_start_iso=start.isoformat(),
        requested_end_iso=(start + HOUR).isoformat(),
        service_description=service,
        caller_name="Ada",
    )


async def _confirm(db, session, intent=None):
    """Stage (optional), arm read-back + caller yes, then persist."""
    if intent is not None:
        staged = await stage_booking(db, session, intent)
        if staged.outcome is not BookingOutcome.DRAFT:
            return staged
    arm_verified_proposal(session)
    return await confirm_booking(db, session)


# --------------------------------------------------------------------------- #
# 1. One date, confirmed once
# --------------------------------------------------------------------------- #
async def test_single_date_confirmed_creates_exactly_one_booking(world):
    db, session = world["db"], world["session"]

    staged = await stage_booking(db, session, _wants(SEPT_20))
    assert staged.outcome is BookingOutcome.DRAFT
    assert world["store"].rows == [], "staging must not write an appointment"
    assert world["adapter"].created == [], "staging must not touch the calendar"

    result = await _confirm(db, session)

    assert result.outcome is BookingOutcome.BOOKED
    assert len(world["store"].live) == 1
    assert world["adapter"].created == [(SEPT_20, SEPT_20 + HOUR)]


# --------------------------------------------------------------------------- #
# 2 & 3. Changing the date before confirming
# --------------------------------------------------------------------------- #
async def test_date_changed_before_confirmation_books_only_the_last_one(world):
    db, session = world["db"], world["session"]

    await stage_booking(db, session, _wants(SEPT_20))
    await stage_booking(db, session, _wants(SEPT_22))
    result = await _confirm(db, session)

    assert result.outcome is BookingOutcome.BOOKED
    assert len(world["store"].live) == 1
    assert world["store"].live[0].start_time == SEPT_22
    assert world["adapter"].created == [(SEPT_22, SEPT_22 + HOUR)]


async def test_unavailable_slot_does_not_replace_the_last_verified_offer(world):
    db, session, adapter = world["db"], world["session"], world["adapter"]

    await _confirm(db, session, _wants(SEPT_20))
    adapter.busy = [(SEPT_22, SEPT_22 + HOUR)]

    result = await stage_booking(db, session, _wants(SEPT_22))

    assert result.outcome is BookingOutcome.CONFLICT
    draft = get_draft(session)
    assert draft.start_iso == SEPT_20.isoformat()
    assert draft.end_iso == (SEPT_20 + HOUR).isoformat()
    assert session.requested_datetime == SEPT_20.isoformat()
    assert session.selected_service == _wants(SEPT_20).service_description


async def test_three_successive_changes_book_only_the_final_date(world):
    db, session = world["db"], world["session"]

    for when in (SEPT_20, SEPT_22, SEPT_24):
        await stage_booking(db, session, _wants(when))
    await _confirm(db, session)

    assert len(world["store"].live) == 1
    assert world["store"].live[0].start_time == SEPT_24
    assert world["adapter"].created == [(SEPT_24, SEPT_24 + HOUR)]


# --------------------------------------------------------------------------- #
# 4. Changing only the time
# --------------------------------------------------------------------------- #
async def test_changing_only_the_time_updates_the_same_intent(world):
    db, session = world["db"], world["session"]
    later = SEPT_20 + timedelta(hours=3)

    await stage_booking(db, session, _wants(SEPT_20))
    booking_id = get_draft(session).booking_id
    await stage_booking(db, session, _wants(later))

    assert get_draft(session).booking_id == booking_id, "same intent, edited"

    await _confirm(db, session)
    assert len(world["store"].live) == 1
    assert world["store"].live[0].start_time == later


# --------------------------------------------------------------------------- #
# 5 & 6. Repeated confirmation
# --------------------------------------------------------------------------- #
async def test_saying_yes_three_times_yields_one_event(world):
    db, session = world["db"], world["session"]

    await stage_booking(db, session, _wants(SEPT_22))
    first = await _confirm(db, session)
    second = await confirm_booking(db, session)
    third = await confirm_booking(db, session)

    assert [r.outcome for r in (first, second, third)] == [BookingOutcome.BOOKED] * 3
    assert first.appointment.id == second.appointment.id == third.appointment.id
    assert len(world["store"].live) == 1
    assert len(world["adapter"].created) == 1
    # The repeat must read as success, never as a clash.
    assert "not a clash" in second.message
    assert "booked" in second.message.lower()


async def test_booking_tool_invoked_twice_yields_one_event(world):
    """A duplicated tool call carrying the same details is not a second booking."""
    db, session = world["db"], world["session"]

    await _confirm(db, session, _wants(SEPT_22))
    await _confirm(db, session, _wants(SEPT_22))

    assert len(world["store"].live) == 1
    assert len(world["adapter"].created) == 1


# --------------------------------------------------------------------------- #
# 7 vs 8. Whose booking is in the way?
# --------------------------------------------------------------------------- #
async def test_slot_held_by_this_session_is_success_not_conflict(world):
    db, session = world["db"], world["session"]

    await _confirm(db, session, _wants(SEPT_22))
    repeat = await confirm_booking(db, session)

    assert repeat.outcome is BookingOutcome.BOOKED
    assert "already confirmed" in repeat.message.lower()
    assert "taken" not in repeat.message.lower()


async def test_slot_held_by_a_different_customer_is_reported_unavailable(world):
    db, session, store = world["db"], world["session"], world["store"]
    # Somebody else's appointment, from another call, already occupies the slot.
    other = Appointment(
        id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        title="Facial",
        start_time=SEPT_22,
        end_time=SEPT_22 + HOUR,
        status=AppointmentStatus.SCHEDULED,
        booking_intent_key="CA-someone-else:intent",
        tenant_id=world["spa"].id,
    )
    store.rows.append(other)

    result = await _confirm(db, session, _wants(SEPT_22))

    assert result.outcome is BookingOutcome.CONFLICT
    assert "unavailable" in result.message.lower() or "different customer" in result.message
    assert len(world["store"].live) == 1, "no appointment was created"
    assert world["adapter"].created == []


# --------------------------------------------------------------------------- #
# 9. Changing the date after the event was already written
# --------------------------------------------------------------------------- #
async def test_date_changed_after_persistence_moves_the_existing_event(world):
    db, session = world["db"], world["session"]
    adapter = world["adapter"]

    await _confirm(db, session, _wants(SEPT_20))
    original_id = world["store"].live[0].id
    assert len(adapter.created) == 1

    await stage_booking(db, session, _wants(SEPT_22))
    result = await _confirm(db, session)

    assert result.outcome is BookingOutcome.RESCHEDULED
    assert len(world["store"].live) == 1, "must not create a second appointment"
    assert world["store"].live[0].id == original_id, "same row, moved"
    assert world["store"].live[0].start_time == SEPT_22
    assert len(adapter.created) == 1, "no second calendar event"
    assert adapter.moved == [("evt-1", SEPT_22, SEPT_22 + HOUR)]


# --------------------------------------------------------------------------- #
# 10 & 11. Cancelling
# --------------------------------------------------------------------------- #
async def test_cancelling_before_confirmation_creates_nothing(world):
    db, session = world["db"], world["session"]

    await stage_booking(db, session, _wants(SEPT_20))
    result = await cancel_booking(db, session)

    assert result.outcome is BookingOutcome.NOT_FOUND
    assert world["store"].rows == []
    assert world["adapter"].created == []
    assert world["adapter"].cancelled == []


async def test_cancelling_after_persistence_cancels_the_right_event(world):
    db, session = world["db"], world["session"]

    await _confirm(db, session, _wants(SEPT_22))
    result = await cancel_booking(db, session)

    assert result.outcome is BookingOutcome.CANCELLED
    assert world["adapter"].cancelled == ["evt-1"]
    assert world["store"].live == [], "the row is no longer active"
    assert session.appointment_id is None


# --------------------------------------------------------------------------- #
# 12. Two genuinely separate appointments
# --------------------------------------------------------------------------- #
async def test_explicit_second_appointment_is_allowed(world):
    db, session = world["db"], world["session"]

    await _confirm(db, session, _wants(SEPT_20))
    # Only an explicit request opens a second intent.
    start_new_intent(session)
    await _confirm(db, session, _wants(SEPT_24))

    assert len(world["store"].live) == 2
    assert {r.start_time for r in world["store"].live} == {SEPT_20, SEPT_24}
    assert len(world["adapter"].created) == 2
    keys = {r.booking_intent_key for r in world["store"].live}
    assert len(keys) == 2, "each appointment carries its own idempotency key"


async def test_intent_keys_are_not_derived_from_the_requested_time(world):
    """Two callers may legitimately hold the same slot where capacity allows,
    so the key must identify the conversation, not the clock."""
    db, session = world["db"], world["session"]

    await _confirm(db, session, _wants(SEPT_20))
    first_key = world["store"].live[0].booking_intent_key
    start_new_intent(session)
    await _confirm(db, session, _wants(SEPT_24))
    second_key = [r for r in world["store"].live if r.start_time == SEPT_24][0].booking_intent_key

    assert first_key != second_key
    assert session.call_sid in first_key and session.call_sid in second_key
    assert SEPT_20.isoformat() not in first_key


# --------------------------------------------------------------------------- #
# Confirmation detection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "utterance",
    ["Yes", "yes please", "yep", "that works", "Go ahead", "book it", "Perfect.", "Yes, book it."],
)
def test_plain_agreement_is_a_confirmation(utterance):
    assert is_affirmative(utterance)


@pytest.mark.parametrize(
    "utterance",
    [
        "Actually, can we do September 22 instead?",
        "yes but make it Tuesday",
        "Yeah, Friday.",
        "Correct, with Sarah.",
        "Yes, but make it Swedish massage instead.",
        "Yes, but make it 4 PM.",
        "Yeah, October 3 instead.",
        "Yes, with Sarah.",
        "no",
        "not that one",
        "can I change it?",
        "",
        "hmm",
        # A sign-off is the caller leaving, not agreeing to be booked.
        "Great, bye",
        "ok never mind",
    ],
)
def test_a_change_of_mind_is_never_a_confirmation(utterance):
    assert not is_affirmative(utterance)


# --------------------------------------------------------------------------- #
# Rescheduling something booked on an earlier call
# --------------------------------------------------------------------------- #
async def test_reschedule_of_a_previous_calls_appointment_moves_it(world):
    """The duplicate spread across two calls: a caller ringing back to move an
    appointment must not end up holding both the old slot and the new one."""
    db, session, store = world["db"], world["session"], world["store"]
    prior = Appointment(
        id=uuid.uuid4(),
        contact_id=world["contact_id"],
        title="Deep tissue massage",
        start_time=SEPT_20,
        end_time=SEPT_20 + HOUR,
        status=AppointmentStatus.SCHEDULED,
        booking_intent_key="CA-yesterday:intent",
        external_booking_id="evt-prior",
        booking_provider="internal",
        tenant_id=world["spa"].id,
    )
    store.rows.append(prior)

    draft = get_draft(session)
    draft.operation_mode = "reschedule"
    draft.target_appointment_id = str(prior.id)
    save_draft(session, draft)

    moving = _wants(SEPT_22)
    moving = moving.model_copy(update={"intent": "reschedule"})
    result = await _confirm(db, session, moving)

    assert result.outcome is BookingOutcome.RESCHEDULED
    assert len(world["store"].live) == 1, "the old slot must not survive alongside the new"
    assert world["store"].live[0].id == prior.id
    assert world["store"].live[0].start_time == SEPT_22
    assert world["adapter"].created == [], "moving is not creating"
    assert world["adapter"].moved == [("evt-prior", SEPT_22, SEPT_22 + HOUR)]


async def test_13_reschedule_provider_failure_keeps_old_appointment(world):
    """Square/provider move failure must not create a second booking or
    report success while the original time remains."""
    db, session, store = world["db"], world["session"], world["store"]
    prior = Appointment(
        id=uuid.uuid4(),
        contact_id=world["contact_id"],
        title="Deep tissue massage",
        start_time=SEPT_20,
        end_time=SEPT_20 + HOUR,
        status=AppointmentStatus.SCHEDULED,
        booking_intent_key="CA-yesterday:intent",
        external_booking_id="evt-prior",
        booking_provider="internal",
        tenant_id=world["spa"].id,
    )
    store.rows.append(prior)

    async def _boom(_booking, _start, _end):
        raise BookingProviderError("Square update failed")

    world["adapter"].move_booking = _boom

    draft = get_draft(session)
    draft.operation_mode = "reschedule"
    draft.target_appointment_id = str(prior.id)
    save_draft(session, draft)

    moving = _wants(SEPT_22).model_copy(update={"intent": "reschedule"})
    result = await _confirm(db, session, moving)

    assert result.outcome is BookingOutcome.ERROR
    assert "rescheduled" not in result.message.lower()
    assert len(store.live) == 1
    assert store.live[0].id == prior.id
    assert store.live[0].start_time == SEPT_20
    assert world["adapter"].created == []


async def test_reschedule_with_nothing_to_move_is_reported_not_silently_booked(world):
    db, session = world["db"], world["session"]

    moving = _wants(SEPT_22).model_copy(update={"intent": "reschedule"})
    result = await _confirm(db, session, moving)

    assert result.outcome is BookingOutcome.NOT_FOUND
    assert world["store"].rows == []
    assert world["adapter"].created == []


# --------------------------------------------------------------------------- #
# Phase 1: confirmation gating — provider success + a valid booking ID are
# BOTH required before anything is reported as booked.
# --------------------------------------------------------------------------- #
async def test_provider_success_with_no_booking_id_is_not_confirmed(world):
    """Case B from the Phase 1 brief: the provider call returns without
    raising, but with no external id. That is not proof of a booking."""
    db, session, adapter = world["db"], world["session"], world["adapter"]
    adapter.create_booking_failure = "no_id"

    result = await _confirm(db, session, _wants(SEPT_20))

    assert result.outcome is BookingOutcome.ERROR
    assert world["store"].live == [], "no appointment may be reported live without a provider id"


async def test_provider_technical_error_is_not_confirmed_and_not_a_slot_conflict(world):
    """Case C: a technical provider failure must map to ERROR, never CONFLICT
    — the caller must never be told their slot became unavailable when the
    real problem was e.g. a customer-creation or network failure."""
    db, session, adapter = world["db"], world["session"], world["adapter"]
    adapter.create_booking_failure = BookingProviderError(
        "Square API request failed: IDEMPOTENCY_KEY_REUSED", code="IDEMPOTENCY_KEY_REUSED"
    )

    result = await _confirm(db, session, _wants(SEPT_20))

    assert result.outcome is BookingOutcome.ERROR
    assert "unavailable" not in result.message.lower()
    assert "no longer available" not in result.message.lower()
    assert world["store"].live == []


async def test_provider_timeout_is_not_confirmed_and_does_not_duplicate_on_retry(world):
    """Case D: an uncertain (timed-out) provider outcome must not be reported
    as booked, and retrying the same confirmation afterward must converge on
    at most one appointment — never a duplicate."""
    db, session, adapter = world["db"], world["session"], world["adapter"]
    adapter.create_booking_failure = asyncio.TimeoutError()

    first = await _confirm(db, session, _wants(SEPT_20))
    assert first.outcome is BookingOutcome.ERROR
    assert world["store"].live == []

    # The provider recovers; the caller (or the agent) retries the same
    # confirmation. Exactly one appointment must exist afterward.
    adapter.create_booking_failure = None
    second = await confirm_booking(db, session)

    assert second.outcome is BookingOutcome.BOOKED
    assert len(world["store"].live) == 1
    assert len(world["adapter"].created) == 1


async def test_genuine_slot_conflict_is_classified_as_conflict_not_a_technical_error(world):
    """The counterpart to the two tests above: when the provider genuinely
    reports the slot as unavailable, that — and only that — is a CONFLICT."""
    db, session, adapter = world["db"], world["session"], world["adapter"]
    adapter.busy = [(SEPT_20, SEPT_20 + HOUR)]

    result = await stage_booking(db, session, _wants(SEPT_20))

    assert result.outcome is BookingOutcome.CONFLICT
    assert world["store"].live == []
    assert world["adapter"].created == []


# --------------------------------------------------------------------------- #
# Mandatory booking-state machine regressions
# --------------------------------------------------------------------------- #
async def test_1_no_confirmation_never_creates_square_booking(world):
    db, session = world["db"], world["session"]
    await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    assert world["adapter"].created == []
    assert session.external_booking_id is None
    assert get_draft(session).appointment_id is None


async def test_2_unavailable_must_never_book(world):
    db, session, adapter = world["db"], world["session"], world["adapter"]
    adapter.busy = [(SEPT_20, SEPT_20 + HOUR)]
    staged = await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    assert staged.outcome is BookingOutcome.CONFLICT
    blocked = await confirm_booking(db, session)
    assert blocked.outcome is not BookingOutcome.BOOKED
    assert adapter.created == []
    assert session.external_booking_id is None
    assert get_draft(session).selected_slot is None


async def test_3_change_service_before_confirmation_clears_hydrolux_slot(world):
    db, session = world["db"], world["session"]
    await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    hydro = get_draft(session)
    assert hydro.selected_slot["service_variation_id"] == "var_hydrolux"
    hydro_rev = hydro.draft_revision
    await stage_booking(db, session, _wants(SEPT_20, "Swedish Massage"))
    swedish = get_draft(session)
    assert swedish.draft_revision > hydro_rev
    assert swedish.service_description == "Swedish Massage"
    assert swedish.selected_slot["service_variation_id"] == "var_swedish"
    assert world["adapter"].created == []


async def test_4_affirmative_plus_service_change_does_not_book(world):
    db, session = world["db"], world["session"]
    await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    arm_verified_proposal(session)
    utterance = "Yes, but make it Swedish Massage instead."
    assert utterance_modifies_booking(utterance)
    assert not is_affirmative(utterance)
    session.add_turn("user", utterance)
    await stage_booking(db, session, _wants(SEPT_20, "Swedish Massage"))
    result = await confirm_booking(db, session)
    assert result.outcome is not BookingOutcome.BOOKED
    assert world["adapter"].created == []
    assert get_draft(session).service_description == "Swedish Massage"


async def test_5_affirmative_plus_time_change_does_not_book_old_time(world):
    db, session = world["db"], world["session"]
    later = SEPT_20 + timedelta(hours=1)
    await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    arm_verified_proposal(session)
    utterance = "Yes, but make it 4 PM."
    assert not is_affirmative(utterance)
    await stage_booking(db, session, _wants(later, "HydroLux facial"))
    result = await confirm_booking(db, session)
    assert result.outcome is not BookingOutcome.BOOKED
    assert world["adapter"].created == []
    assert get_draft(session).start_iso == later.isoformat()


async def test_6_stale_revision_hard_reject(world):
    db, session = world["db"], world["session"]
    await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    arm_verified_proposal(session)
    stale = get_draft(session)
    stale_rev = stale.draft_revision
    stale_fp = stale.verified_fingerprint
    await stage_booking(db, session, _wants(SEPT_20, "Swedish Massage"))
    draft = get_draft(session)
    draft.read_back = True
    draft.read_back_revision = stale_rev
    draft.read_back_fingerprint = stale_fp
    draft.confirmation_authorized = True
    draft.selected_slot_revision = stale_rev
    session.entities["caller_confirmed_revision"] = stale_rev
    session.entities["caller_confirmed_fingerprint"] = stale_fp
    save_draft(session, draft)
    session.booking_status = "awaiting_confirmation"
    result = await confirm_booking(db, session)
    assert result.outcome is not BookingOutcome.BOOKED
    assert world["adapter"].created == []


async def test_7_final_confirmation_creates_exactly_one_swedish_booking(world):
    db, session = world["db"], world["session"]
    oct3 = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)
    await stage_booking(db, session, _wants(oct3, "Swedish Massage"))
    result = await _confirm(db, session)
    assert result.outcome is BookingOutcome.BOOKED
    assert len(world["adapter"].created) == 1
    ctx = world["adapter"].created_contexts[0]
    assert "swedish" in (ctx.service_description or "").lower()
    assert (ctx.selected_slot or {}).get("service_variation_id") == "var_swedish"


async def test_8_final_payload_uses_current_swedish_variation(world):
    db, session = world["db"], world["session"]
    await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    await stage_booking(db, session, _wants(SEPT_20, "Swedish Massage"))
    result = await _confirm(db, session)
    assert result.outcome is BookingOutcome.BOOKED
    slot = world["adapter"].created_contexts[0].selected_slot or {}
    assert slot.get("service_variation_id") == "var_swedish"
    assert slot.get("service_variation_id") != "var_hydrolux"


async def test_9_changing_service_invalidates_old_alternatives(world):
    db, session, adapter = world["db"], world["session"], world["adapter"]
    adapter.busy = [(SEPT_20, SEPT_20 + HOUR)]
    await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    draft = get_draft(session)
    draft.alternative_slots = [
        {"start": (SEPT_20 + timedelta(hours=1)).isoformat(), "service_variation_id": "var_hydrolux"},
        {"start": (SEPT_20 + timedelta(hours=2)).isoformat(), "service_variation_id": "var_hydrolux"},
        {"start": (SEPT_20 + timedelta(hours=3)).isoformat(), "service_variation_id": "var_hydrolux"},
    ]
    save_draft(session, draft)
    adapter.busy = []
    chosen = SEPT_20 + timedelta(hours=2)
    await stage_booking(db, session, _wants(chosen, "HydroLux facial"))
    assert get_draft(session).selected_slot is not None
    await stage_booking(db, session, _wants(chosen, "Swedish Massage"))
    updated = get_draft(session)
    for slot in updated.alternative_slots or []:
        assert slot.get("service_variation_id") != "var_hydrolux"
    assert (updated.selected_slot or {}).get("service_variation_id") == "var_swedish"


async def test_10_unavailable_speech_path_cannot_create_booking(world):
    db, session, adapter = world["db"], world["session"], world["adapter"]
    adapter.busy = [(SEPT_20, SEPT_20 + HOUR)]
    staged = await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
    assert staged.outcome is BookingOutcome.CONFLICT
    arm_verified_proposal(session)
    sneak = await confirm_booking(db, session)
    assert sneak.outcome is not BookingOutcome.BOOKED
    assert adapter.created == []


async def test_propose_one_hundred_times_never_calls_create_booking(world):
    db, session, adapter = world["db"], world["session"], world["adapter"]
    for _ in range(100):
        result = await stage_booking(db, session, _wants(SEPT_20, "HydroLux facial"))
        assert result.outcome is BookingOutcome.DRAFT
        assert result.appointment is None
    assert adapter.created == []
    assert adapter.created_contexts == []
