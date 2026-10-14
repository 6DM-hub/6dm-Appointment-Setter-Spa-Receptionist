"""Brick 1: "keep it" / "never mind" must preserve the booking, never add one.

The bug (found in independent verification of the P0 repairs): after a
booking, CARA asked "change it, or keep it and add another?". The answer
"keep it" was classified as a request for an ADDITIONAL appointment, a new
3 PM slot was offered as "3 PM is available... book that?", and one "yes"
left two appointments in Square. "Never mind" while a change was being
arranged also left that change confirmable.

Rules pinned here, against the real realtime handler, the real booking
engine and the stateful in-memory Square (SIMULATED, not real Square):
* "keep it", "never mind", "leave it alone", "no, keep the original" keep the
  booking exactly as it is: nothing is proposed, staged, changed or created,
  and the caller is told so;
* unclear answers are asked again and change nothing;
* an explicitly requested additional appointment is announced AS additional
  before it can be confirmed, and cannot be confirmed in the same breath.
"""
import json
from datetime import timedelta

import pytest

from app.services.appointment_booking_service import (
    BookingOutcome,
    confirm_booking,
    stage_booking,
)
from app.services.booking_state import arm_verified_proposal, get_draft
from app.services.grok_service import AppointmentIntent
from tests.square_world import THU_2PM_UTC, call, local, voice_for, wire

HOUR = timedelta(hours=1)
THU_3PM_UTC = THU_2PM_UTC + HOUR
THU_4PM_UTC = THU_2PM_UTC + 2 * HOUR
TWO_PM_Z = "2026-10-22T19:00:00Z"

KEEP_ANSWERS = [
    "Keep it.",
    "Never mind.",
    "Never mind, keep it.",
    "Leave it alone.",
    "No, keep the original.",
    "No, just keep it the way it is.",
    "Leave it as it is.",
    "Forget it.",
    "Don't change it.",
]
UNCLEAR_ANSWERS = ["Yes please.", "The new time.", "Okay.", "Hmm, I'm not sure."]


@pytest.fixture
def world(monkeypatch):
    w = wire(monkeypatch)
    for start in (THU_2PM_UTC, THU_3PM_UTC):
        w.square.open(start, "var_swedish_60", "TM_ANA")
        w.square.open(start, "var_swedish_60", "TM_BEA")
    w.square.open(THU_4PM_UTC, "var_facial_60", "TM_BEA")
    w.monkeypatch = monkeypatch
    return w


async def _voice_after_booking(world):
    """A call that has just booked Thursday 2 PM Swedish Massage in Square."""
    session = call(world.spa)
    staged = await stage_booking(world.db, session, AppointmentIntent(
        intent="schedule", confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(THU_2PM_UTC), service_description="Swedish Massage",
    ))
    assert staged.outcome is BookingOutcome.DRAFT, staged.message
    arm_verified_proposal(session)
    booked = await confirm_booking(world.db, session)
    assert booked.outcome is BookingOutcome.BOOKED, booked.message
    session.booking_status = "booked"
    voice = voice_for(world, session, world.monkeypatch)
    voice._booking_completed_turn = 3
    voice._user_turn_count = 4
    return voice, booked.appointment


async def _caller_says(voice, utterance, **proposal):
    voice._user_turn_count += 1
    voice.session.add_turn("user", utterance)
    return json.loads(await voice._run_propose_appointment(json.dumps(proposal)))


async def _caller_says_yes_to_whatever_was_read_back(world, voice):
    """Worst case: the caller confirms the current proposal, whatever it is."""
    arm_verified_proposal(voice.session)
    return await confirm_booking(world.db, voice.session)


def _square_starts(world):
    return sorted(b["start_at"] for b in world.square.active())


THREE_PM = {"requested_start_iso": local(THU_3PM_UTC), "service_description": "Swedish Massage",
            "caller_name": "Ada Lovelace"}


# --------------------------------------------------------------------------- #
# Keep answers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("answer", KEEP_ANSWERS)
async def test_keep_answer_preserves_the_booking_and_books_nothing(world, answer):
    voice, row = await _voice_after_booking(world)
    asked = await _caller_says(voice, "Actually, can we do 3?", **THREE_PM)
    assert asked["status"] == "needs_clarification"

    # The model re-sends the 3 PM proposal along with the caller's answer.
    out = await _caller_says(voice, answer, **THREE_PM)

    assert out["status"] == "kept_existing", out
    assert out["booked"] is False
    assert "stays exactly as it is" in out["spoken"]
    assert "Thursday at 2 PM" in out["spoken"]
    draft = get_draft(voice.session)
    assert draft.appointment_id == str(row.id), "no new booking intent may be opened"
    assert draft.start_iso and "14:00" in draft.start_iso

    result = await _caller_says_yes_to_whatever_was_read_back(world, voice)
    assert result.appointment is row
    assert _square_starts(world) == [TWO_PM_Z]
    assert len(world.square.creates) == 1 and world.square.updates == []
    assert world.store.live == [row]


async def test_never_mind_during_a_staged_change_leaves_square_untouched(world):
    voice, row = await _voice_after_booking(world)
    staged = await _caller_says(voice, "Can we move it to 3 instead?", **THREE_PM)
    assert staged["status"] == "draft" and staged["operation"] == "reschedule"

    out = await _caller_says(voice, "Never mind, keep it.", **THREE_PM)
    assert out["status"] == "kept_existing", out

    result = await _caller_says_yes_to_whatever_was_read_back(world, voice)
    assert result.appointment is row
    assert world.square.updates == [], "the abandoned 3 PM change must not be written"
    assert _square_starts(world) == [TWO_PM_Z]
    assert row.start_time == THU_2PM_UTC


async def test_repeated_confirmations_after_keep_write_nothing(world):
    voice, row = await _voice_after_booking(world)
    await _caller_says(voice, "Actually, can we do 3?", **THREE_PM)
    assert (await _caller_says(voice, "No, keep the original.", **THREE_PM))["status"] == "kept_existing"

    for _ in range(3):  # duplicated tool calls, retries, repeated "yes"
        result = await _caller_says_yes_to_whatever_was_read_back(world, voice)
        assert result.outcome in {BookingOutcome.BOOKED, BookingOutcome.RESCHEDULED}
        assert result.appointment is row

    assert len(world.square.creates) == 1 and world.square.updates == []
    assert _square_starts(world) == [TWO_PM_Z]


async def test_keep_through_the_start_new_appointment_tool_opens_nothing(world):
    voice, row = await _voice_after_booking(world)
    voice._user_turn_count += 1
    voice.session.add_turn("user", "Never mind, keep it.")

    out = json.loads(await voice._run_start_new_appointment())

    assert out["status"] == "kept_existing"
    assert get_draft(voice.session).appointment_id == str(row.id)
    assert len(world.square.creates) == 1


# --------------------------------------------------------------------------- #
# Unclear answers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("answer", UNCLEAR_ANSWERS)
async def test_unclear_answer_is_asked_again_and_changes_nothing(world, answer):
    voice, row = await _voice_after_booking(world)
    await _caller_says(voice, "Actually, can we do 3?", **THREE_PM)

    out = await _caller_says(voice, answer, **THREE_PM)

    assert out["status"] == "needs_clarification", out
    assert get_draft(voice.session).appointment_id == str(row.id)
    result = await _caller_says_yes_to_whatever_was_read_back(world, voice)
    assert result.appointment is row
    assert len(world.square.creates) == 1 and world.square.updates == []


# --------------------------------------------------------------------------- #
# A genuinely additional appointment is announced as additional
# --------------------------------------------------------------------------- #
async def test_additional_appointment_is_announced_before_the_booking_question(world):
    voice, row = await _voice_after_booking(world)

    out = await _caller_says(
        voice, "Keep it, and book an additional appointment for a facial at 4.",
        requested_start_iso=local(THU_4PM_UTC), service_description="Signature Facial",
        caller_name="Ada Lovelace",
    )

    assert out["status"] == "draft", out
    spoken = out["spoken"]
    assert "this would be an additional appointment" in spoken
    assert "Swedish Massage on Thursday at 2 PM stays booked" in spoken
    assert spoken.index("additional appointment") < spoken.index("Would you like me to book")

    result = await _caller_says_yes_to_whatever_was_read_back(world, voice)
    assert result.outcome is BookingOutcome.BOOKED
    assert _square_starts(world) == [TWO_PM_Z, "2026-10-22T21:00:00Z"]


async def test_additional_appointment_cannot_be_confirmed_before_the_notice(world):
    voice, row = await _voice_after_booking(world)
    voice._user_turn_count += 1
    voice.session.add_turn("user", "I'd like to book a second appointment.")
    assert json.loads(await voice._run_start_new_appointment())["status"] == "new_intent"
    # The new request is staged by a path that did not speak the notice.
    staged = await stage_booking(world.db, voice.session, AppointmentIntent(
        intent="schedule", confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(THU_4PM_UTC), service_description="Signature Facial",
    ))
    assert staged.outcome is BookingOutcome.DRAFT
    arm_verified_proposal(voice.session)
    voice._user_turn_count += 1
    voice.session.add_turn("user", "Yes, book it.")

    first = json.loads(await voice._run_confirm_appointment())
    assert first["status"] == "additional_notice_required", first
    assert first["booked"] is False
    assert "this would be an additional appointment" in first["notice"]
    again = json.loads(await voice._run_confirm_appointment())  # same turn retry
    assert again["booked"] is False
    assert len(world.square.creates) == 1, "nothing may be booked before the caller answers"

    voice._user_turn_count += 1
    voice.session.add_turn("user", "Yes.")
    arm_verified_proposal(voice.session)
    booked = json.loads(await voice._run_confirm_appointment())
    assert booked["status"] == "booked", booked
    assert _square_starts(world) == [TWO_PM_Z, "2026-10-22T21:00:00Z"]
