"""P0 #2: a reschedule moves the appointment the caller chose, never another.

The production bug: in a call that had just booked appointment A, the caller
asked to move an OLDER appointment B (picked from the backend's lookup list).
`confirm_booking` loaded the row for this call's intent key first, found A,
and moved A (including the Square write-back) while telling the caller "the
same appointment was moved". B stayed where it was.

These tests use the real engine and the real Square adapter against a
stateful in-memory Square, and check both bookings in Square afterwards.
"""
from datetime import timedelta

import pytest

from app.models import AppointmentStatus
from app.services.appointment_booking_service import (
    BookingOutcome,
    confirm_booking,
    stage_booking,
)
from app.services.booking_state import arm_verified_proposal, get_draft, save_draft
from app.services.grok_service import AppointmentIntent
from tests.square_world import THU_2PM_UTC, call, local, wire

HOUR = timedelta(hours=1)
FRI_3PM_UTC = THU_2PM_UTC + timedelta(days=1, hours=1)   # Friday 3pm Chicago
FRI_5PM_UTC = FRI_3PM_UTC + 2 * HOUR


def _ask(service, start, intent="schedule"):
    return AppointmentIntent(
        intent=intent, confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(start), service_description=service,
    )


async def _book(world, session, service, start):
    staged = await stage_booking(world.db, session, _ask(service, start))
    assert staged.outcome is BookingOutcome.DRAFT, staged.message
    arm_verified_proposal(session)
    booked = await confirm_booking(world.db, session)
    assert booked.outcome is BookingOutcome.BOOKED, booked.message
    return booked.appointment


def _select_for_reschedule(session, appointment_id, offered):
    """What `propose_appointment(operation=reschedule, appointment_id=...)` records."""
    draft = get_draft(session)
    draft.operation_mode = "reschedule"
    draft.target_appointment_id = str(appointment_id)
    draft.offered_appointment_ids = [str(a) for a in offered]
    save_draft(session, draft)


def _square_start(world, booking_id):
    return world.square.bookings[booking_id]["start_at"]


@pytest.fixture
def world(monkeypatch):
    w = wire(monkeypatch)
    w.square.open(THU_2PM_UTC, "var_facial_60", "TM_BEA")
    w.square.open(FRI_3PM_UTC, "var_swedish_60", "TM_ANA")
    w.square.open(FRI_5PM_UTC, "var_swedish_60", "TM_ANA")
    # Bea could also take A's facial on Friday at 5pm, so moving the WRONG
    # appointment would succeed silently in Square. That is the dangerous case.
    w.square.open(FRI_5PM_UTC, "var_facial_60", "TM_BEA")
    return w


@pytest.fixture
async def two_bookings(world):
    # Yesterday's call booked B: Friday 3pm Swedish Massage.
    earlier = call(world.spa, "CA-yesterday")
    b_row = await _book(world, earlier, "Swedish Massage", FRI_3PM_UTC)
    # Today's call books A: Thursday 2pm Signature Facial.
    today = call(world.spa, "CA-today")
    a_row = await _book(world, today, "Signature Facial", THU_2PM_UTC)
    assert (a_row.external_booking_id, b_row.external_booking_id) == ("sq_2", "sq_1")
    return today, a_row, b_row


async def test_moving_an_older_appointment_moves_that_appointment(world, two_bookings):
    session, a_row, b_row = two_bookings
    _select_for_reschedule(session, b_row.id, [a_row.id, b_row.id])

    staged = await stage_booking(world.db, session, _ask("Swedish Massage", FRI_5PM_UTC, "reschedule"))
    assert staged.outcome is BookingOutcome.DRAFT, staged.message
    arm_verified_proposal(session)
    result = await confirm_booking(world.db, session)

    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert result.appointment is b_row
    # Square: B moved, A untouched.
    assert _square_start(world, "sq_1") == "2026-10-23T22:00:00Z"
    assert _square_start(world, "sq_2") == "2026-10-22T19:00:00Z"
    assert [path for path, _ in world.square.updates] == ["/v2/bookings/sq_1"]
    # Local rows agree, and nothing new was created.
    assert b_row.start_time == FRI_5PM_UTC
    assert a_row.start_time == THU_2PM_UTC
    assert len(world.square.creates) == 2 and len(world.store.live) == 2


async def test_retrying_the_move_does_not_touch_either_appointment_again(world, two_bookings):
    session, a_row, b_row = two_bookings
    _select_for_reschedule(session, b_row.id, [a_row.id, b_row.id])
    await stage_booking(world.db, session, _ask("Swedish Massage", FRI_5PM_UTC, "reschedule"))
    arm_verified_proposal(session)
    assert (await confirm_booking(world.db, session)).outcome is BookingOutcome.RESCHEDULED

    for _ in range(2):
        arm_verified_proposal(session)
        again = await confirm_booking(world.db, session)
        assert again.outcome is BookingOutcome.RESCHEDULED
        assert again.appointment is b_row

    assert len(world.square.updates) == 1
    assert _square_start(world, "sq_2") == "2026-10-22T19:00:00Z"
    assert len(world.square.creates) == 2


async def test_selected_appointment_gone_changes_nothing(world, two_bookings):
    session, a_row, b_row = two_bookings
    _select_for_reschedule(session, b_row.id, [a_row.id, b_row.id])
    await stage_booking(world.db, session, _ask("Swedish Massage", FRI_5PM_UTC, "reschedule"))
    b_row.status = AppointmentStatus.CANCELLED  # cancelled by staff meanwhile

    arm_verified_proposal(session)
    result = await confirm_booking(world.db, session)

    assert result.outcome is BookingOutcome.NOT_FOUND
    assert world.square.updates == []
    assert a_row.start_time == THU_2PM_UTC and len(world.square.creates) == 2


async def test_moving_this_calls_own_booking_still_works(world, two_bookings):
    session, a_row, _b_row = two_bookings
    world.square.open(THU_2PM_UTC + HOUR, "var_facial_60", "TM_BEA")
    _select_for_reschedule(session, a_row.id, [a_row.id])

    await stage_booking(world.db, session, _ask("Signature Facial", THU_2PM_UTC + HOUR, "reschedule"))
    arm_verified_proposal(session)
    result = await confirm_booking(world.db, session)

    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert result.appointment is a_row
    assert _square_start(world, "sq_2") == "2026-10-22T20:00:00Z"
    assert _square_start(world, "sq_1") == "2026-10-23T20:00:00Z"


# --------------------------------------------------------------------------- #
# The voice tool: no appointment id, several upcoming appointments -> ask
# --------------------------------------------------------------------------- #
async def test_voice_move_request_without_an_id_asks_which_appointment(world, two_bookings, monkeypatch):
    import json

    from tests.square_world import voice_for

    session, a_row, b_row = two_bookings
    session.booking_status = "booked"
    voice = voice_for(world, session, monkeypatch)
    voice._booking_completed_turn = 3
    voice._user_turn_count = 4
    voice.session.add_turn("user", "Oh, and can you move my Friday massage to 5?")

    out = json.loads(await voice._run_propose_appointment(json.dumps({
        "operation": "reschedule",
        "requested_start_iso": local(FRI_5PM_UTC),
        "service_description": "Swedish Massage",
    })))

    assert out["status"] == "needs_appointment_selection"
    assert {o["appointment_id"] for o in out["appointment_options"]} == {str(a_row.id), str(b_row.id)}
    assert world.square.updates == []

    # The caller picks the Friday appointment; only that one moves.
    voice._user_turn_count += 1
    voice.session.add_turn("user", "The Friday one.")
    out = json.loads(await voice._run_propose_appointment(json.dumps({
        "operation": "reschedule",
        "appointment_id": str(b_row.id),
        "requested_start_iso": local(FRI_5PM_UTC),
        "service_description": "Swedish Massage",
    })))
    assert out["status"] == "draft", out
    arm_verified_proposal(session)
    result = await confirm_booking(world.db, session)

    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert result.appointment is b_row
    assert _square_start(world, "sq_1") == "2026-10-23T22:00:00Z"
    assert _square_start(world, "sq_2") == "2026-10-22T19:00:00Z"
