"""Scratch only: both real voice dispatchers must refuse an elapsed start."""
from datetime import datetime, timedelta

import pytest

from app.services import appointment_booking_service as booking
from app.services.booking_state import get_draft
from tests.test_brick1_booking_change_guard import Phone, LegacyCall, make_world, T2, local, _intent


@pytest.mark.parametrize("path", ["realtime", "legacy"])
@pytest.mark.parametrize("business", ["spa", "hvac", "dental"])
async def test_elapsed_start_never_creates_square_booking(monkeypatch, path, business):
    world = make_world(monkeypatch, business)
    clock = [T2 - timedelta(minutes=10)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)
    monkeypatch.setattr(booking, "datetime", Clock)
    if path == "realtime":
        phone = Phone(world)
        phone.voice._now_provider = lambda tz: clock[0].astimezone(tz)
        phone.voice._pending_caller = f"I'd like {world.first} Thursday at 2 PM. My name is Ada Lovelace."
        phone.voice._flush_caller_turn()
        result = await phone.propose(requested_start_iso=local(T2), service_description=world.first,
                                     caller_name="Ada Lovelace", operation="schedule")
        assert result["status"] == "draft"
    else:
        phone = LegacyCall(world, monkeypatch)
        await phone.says(f"I'd like {world.first} Thursday at 2 PM. My name is Ada Lovelace.",
                         intent=_intent("schedule", T2, world.first),
                         reply="Thursday at 2 PM is open. Would you like me to book that?")
        assert get_draft(phone.session).read_back
    clock[0] = T2 + timedelta(minutes=30)
    await phone.says("Yes, please.")
    assert not world.square.creates, "ELAPSED START REACHED SQUARE CREATE"
    assert not world.store.live
    assert not get_draft(phone.session).confirmation_authorized

from sqlalchemy import select
from app.models import Appointment
from app.services.booking_state import mark_read_back, record_pure_confirmation
from tests.test_cara_native_double_booking import rubyz_db, _stage, _slot_start

async def test_delayed_consent_cannot_book_an_appointment_that_has_already_started(rubyz_db, monkeypatch):
    db, spa = rubyz_db
    start = _slot_start()
    clock = [start - timedelta(minutes=10)]
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            current = clock[0]
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)
    monkeypatch.setattr(booking, "datetime", Later)
    session, staged = await _stage(db, spa, start, 7)
    assert staged.outcome is booking.BookingOutcome.DRAFT
    # Read-back occurs ten minutes before start. Forty minutes pass before
    # the caller gives a pure yes to that unchanged read-back.
    mark_read_back(session)
    clock[0] = start + timedelta(minutes=30)
    assert record_pure_confirmation(session)
    result = await booking.confirm_booking(db, session)
    rows = list((await db.execute(select(Appointment))).scalars())
    assert not rows, "ELAPSED OFFER CREATED AN APPOINTMENT IN THE PAST: " + str(result.outcome)
