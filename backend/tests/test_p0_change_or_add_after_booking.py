"""P0 #3: after a booking, CARA must know "change it" from "add another".

The production bug: once a booking succeeded, a later proposal without an
explicit `operation` (or with the model's guess "schedule") opened a brand new
booking intent. "Actually, can we do 3 o'clock instead?" therefore produced a
SECOND appointment after the caller said yes, leaving the first in place.

Rules these tests pin down:
* The caller's own words decide. "instead / move / change / I meant" changes
  the existing appointment; "another / also / a second / for my wife" adds one.
* Unclear wording, softer cues ("anything later?"), or the caller's words and
  the model's label disagreeing is ASKED about, and nothing changes until the
  caller answers. Answers ("keep it", "update it") are read as answers, and
  follow-up tweaks to a chosen change are not asked about again.
* A repeat of the booked details, a retried tool call, or a misrouted request
  can never create a second identical appointment.

The end-to-end cases drive the real realtime `propose_appointment` handler
into the real booking engine and the real Square adapter, backed by the
stateful in-memory Square in `tests/square_world.py`.
"""
import json
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from app.services import xai_realtime
from app.services.appointment_booking_service import (
    BookingOutcome,
    confirm_booking,
    stage_booking,
)
from app.services.booking_state import (
    BOOKING_CHANGE_CLARIFICATION,
    arm_verified_proposal,
    classify_change_question_answer,
    classify_post_booking_request,
    get_draft,
    start_new_intent,
)
from app.services.grok_service import AppointmentIntent
from app.services.xai_realtime import XAIVoiceSession
from tests.square_world import THU_2PM_UTC, call, local, wire

HOUR = timedelta(hours=1)
THU_3PM_UTC = THU_2PM_UTC + HOUR


# --------------------------------------------------------------------------- #
# The classifier
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("utterance,kind", [
    ("Actually, can we do 3 o'clock instead?", "modify"),
    ("Oh wait, could I come Friday at 2 instead?", "modify"),
    ("Sorry, I meant Thursday, not Wednesday.", "modify"),
    ("I can't make 2, how about 4?", "modify"),
    ("Could we push it to 4?", "modify"),
    ("Make it a facial instead.", "modify"),
    ("Can I also book a facial for my wife?", "additional"),
    ("I'd like to book a second massage on Sunday.", "additional"),
    ("Another one for Saturday, please.", "additional"),
    ("Keep it and book an additional one.", "additional"),
    ("Could I get a Signature Facial at 4, or is there anything later?", "maybe_modify"),
    ("Do you have anything earlier for a facial?", "maybe_modify"),
    ("I'd rather do 3.", "maybe_modify"),
    ("Actually, can we do 3?", "ambiguous"),
    ("Can I book a different time instead?", "ambiguous"),
    ("Also, can we move it to 4?", "ambiguous"),
    ("Yes.", "ambiguous"),
])
def test_caller_words_decide_change_or_add(utterance, kind):
    assert classify_post_booking_request(utterance) == kind


@pytest.mark.parametrize("answer,kind", [
    ("Keep it.", "additional"),
    ("A new appointment.", "additional"),
    ("Make a new booking.", "additional"),
    ("Keep it and add another.", "additional"),
    ("Change it.", "modify"),
    ("Update it.", "modify"),
    ("Modify it.", "modify"),
    ("Replace it.", "modify"),
    ("The first one.", "modify"),
    ("No, change the existing one.", "modify"),
    ("Yes please.", "ambiguous"),
])
def test_answers_to_the_question_are_read_as_answers(answer, kind):
    assert classify_change_question_answer(answer) == kind


# --------------------------------------------------------------------------- #
# End-to-end harness: realtime handler -> engine -> Square
# --------------------------------------------------------------------------- #
@pytest.fixture
def world(monkeypatch):
    w = wire(monkeypatch)
    w.square.open(THU_2PM_UTC, "var_swedish_60", "TM_ANA")
    w.square.open(THU_2PM_UTC, "var_swedish_60", "TM_BEA")
    w.square.open(THU_3PM_UTC, "var_swedish_60", "TM_ANA")
    w.square.open(THU_3PM_UTC, "var_swedish_60", "TM_BEA")
    w.square.open(THU_3PM_UTC + HOUR, "var_facial_60", "TM_BEA")

    @asynccontextmanager
    async def _db():
        yield w.db

    async def _upcoming(_db, _session, limit=10):
        return list(w.store.live)[:limit]

    monkeypatch.setattr(xai_realtime, "AsyncSessionLocal", _db)
    monkeypatch.setattr(xai_realtime, "lookup_upcoming_appointments", _upcoming)
    return w


async def _voice_after_booking(world):
    """A call that has just booked a 2pm Swedish Massage with Square."""
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

    voice = XAIVoiceSession(session.call_sid, session)

    async def _noop(*_a, **_k):
        return None

    voice._send = _noop
    voice._cancel_active_response = _noop
    voice._persist_session = _noop
    voice._arm_availability_hold = _noop
    voice._booking_completed_turn = 3
    voice._user_turn_count = 4
    return voice, booked.appointment


async def _caller_says(voice, utterance, **proposal):
    voice._user_turn_count += 1
    voice.session.add_turn("user", utterance)
    return json.loads(await voice._run_propose_appointment(json.dumps(proposal)))


async def _caller_confirms(world, voice):
    arm_verified_proposal(voice.session)
    return await confirm_booking(world.db, voice.session)


def _square_starts(world):
    return sorted(b["start_at"] for b in world.square.active())


# --------------------------------------------------------------------------- #
# "instead" changes the one existing appointment
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model_operation", [None, "reschedule"])
async def test_time_change_after_booking_moves_the_same_appointment(world, model_operation):
    voice, row = await _voice_after_booking(world)
    proposal = {"requested_start_iso": local(THU_3PM_UTC), "service_description": "Swedish Massage"}
    if model_operation:
        proposal["operation"] = model_operation

    out = await _caller_says(voice, "Actually, can we do 3 o'clock instead?", **proposal)
    assert out["status"] == "draft", out
    assert out["operation"] == "reschedule"
    assert out["target_appointment_id"] == str(row.id)

    result = await _caller_confirms(world, voice)
    assert result.outcome is BookingOutcome.RESCHEDULED, result.message
    assert _square_starts(world) == ["2026-10-22T20:00:00Z"]
    assert len(world.square.creates) == 1, "a change must never create a second booking"
    assert world.store.live == [row]


async def test_model_label_disagreeing_with_instead_is_asked_not_guessed(world):
    voice, row = await _voice_after_booking(world)
    out = await _caller_says(voice, "Actually, can we do 3 o'clock instead?",
                             operation="schedule",  # the model's wrong label
                             requested_start_iso=local(THU_3PM_UTC),
                             service_description="Swedish Massage")
    assert out["status"] == "needs_clarification"
    assert world.square.updates == [] and len(world.square.creates) == 1

    out = await _caller_says(voice, "Change it.", operation="schedule",
                             requested_start_iso=local(THU_3PM_UTC),
                             service_description="Swedish Massage")
    assert out["status"] == "draft" and out["operation"] == "reschedule"
    result = await _caller_confirms(world, voice)
    assert result.outcome is BookingOutcome.RESCHEDULED
    assert _square_starts(world) == ["2026-10-22T20:00:00Z"]
    assert len(world.square.creates) == 1


# --------------------------------------------------------------------------- #
# Unclear wording: ask first, change nothing
# --------------------------------------------------------------------------- #
async def test_unclear_request_is_asked_about_and_changes_nothing(world, monkeypatch):
    voice, row = await _voice_after_booking(world)
    staged = []
    monkeypatch.setattr(xai_realtime, "stage_booking",
                        lambda *a, **k: staged.append(a) or pytest.fail("must not stage"))
    before = get_draft(voice.session).to_dict()

    out = await _caller_says(voice, "Actually, can we do 3?",
                             requested_start_iso=local(THU_3PM_UTC),
                             service_description="Swedish Massage")

    assert out["status"] == "needs_clarification"
    assert out["booked"] is False
    assert "change your Swedish Massage on Thursday at 2 PM" in out["spoken"]
    assert "additional appointment" in out["spoken"]
    assert staged == []
    assert get_draft(voice.session).to_dict() == before
    assert voice.session.entities.get(BOOKING_CHANGE_CLARIFICATION)
    assert _square_starts(world) == ["2026-10-22T19:00:00Z"] and world.square.updates == []


@pytest.mark.parametrize("utterance", [
    "Could I get a Signature Facial at 4, or is there anything later?",
    "Do you have anything earlier for a Signature Facial?",
])
async def test_soft_cue_with_a_new_service_never_replaces_the_booking(world, utterance):
    """Reviewer case: "later"/"earlier" used to swap the booked massage for a facial."""
    voice, row = await _voice_after_booking(world)
    out = await _caller_says(voice, utterance, operation="schedule",
                             requested_start_iso=local(THU_3PM_UTC + HOUR),
                             service_description="Signature Facial")
    assert out["status"] == "needs_clarification"
    assert world.square.updates == []
    assert world.square.bookings["sq_1"]["appointment_segments"][0]["service_variation_id"] == "var_swedish_60"


async def test_keep_it_answer_books_an_additional_appointment(world):
    voice, row = await _voice_after_booking(world)
    proposal = {"requested_start_iso": local(THU_3PM_UTC + HOUR), "service_description": "Signature Facial",
                "caller_name": "Ada Lovelace"}
    assert (await _caller_says(voice, "Could I get a facial at 4, or anything later?",
                               operation="schedule", **proposal))["status"] == "needs_clarification"
    out = await _caller_says(voice, "Keep it.", operation="schedule", **proposal)
    assert out["status"] == "draft" and out["operation"] == "schedule"
    result = await _caller_confirms(world, voice)
    assert result.outcome is BookingOutcome.BOOKED, result.message
    assert _square_starts(world) == ["2026-10-22T19:00:00Z", "2026-10-22T21:00:00Z"]


async def test_question_names_the_booked_time_and_is_rephrased_if_unanswered(world):
    voice, _row = await _voice_after_booking(world)
    proposal = {"requested_start_iso": local(THU_3PM_UTC), "service_description": "Swedish Massage"}
    first = await _caller_says(voice, "Actually, can we do 3?", **proposal)
    assert "Thursday at 2 PM" in first["spoken"] and "3 PM" not in first["spoken"]
    second = await _caller_says(voice, "Yes please.", **proposal)
    assert second["status"] == "needs_clarification"
    assert "'change it' or 'add a new one'" in second["spoken"]
    assert world.square.updates == [] and len(world.square.creates) == 1


async def test_asking_twice_in_one_turn_speaks_the_question_once(world):
    voice, _row = await _voice_after_booking(world)
    proposal = {"requested_start_iso": local(THU_3PM_UTC), "service_description": "Swedish Massage"}
    first = await _caller_says(voice, "Actually, can we do 3?", **proposal)
    assert first["spoken"]
    again = json.loads(await voice._run_propose_appointment(json.dumps(proposal)))  # same turn
    assert again["status"] == "awaiting_clarification" and again["spoken"] is None


async def test_follow_up_tweaks_to_a_chosen_change_are_not_asked_again(world):
    world.square.open(THU_3PM_UTC + HOUR, "var_swedish_60", "TM_ANA")
    world.square.open(THU_3PM_UTC + HOUR, "var_swedish_60", "TM_BEA")
    voice, row = await _voice_after_booking(world)
    out = await _caller_says(voice, "Can we move it to 3 instead?",
                             requested_start_iso=local(THU_3PM_UTC), service_description="Swedish Massage")
    assert out["status"] == "draft" and out["operation"] == "reschedule"

    out = await _caller_says(voice, "Hmm, how about 4 then?",
                             requested_start_iso=local(THU_3PM_UTC + HOUR), service_description="Swedish Massage")
    assert out["status"] == "draft" and out["operation"] == "reschedule"

    result = await _caller_confirms(world, voice)
    assert result.outcome is BookingOutcome.RESCHEDULED
    assert _square_starts(world) == ["2026-10-22T21:00:00Z"] and len(world.square.creates) == 1


async def test_confirming_a_pending_change_is_not_reported_as_already_booked(world):
    """Reviewer case: a restated pending change returned "already_booked" while
    Square still held the old time."""
    voice, row = await _voice_after_booking(world)
    proposal = {"requested_start_iso": local(THU_3PM_UTC), "service_description": "Swedish Massage"}
    assert (await _caller_says(voice, "Can we move it to 3 instead?", **proposal))["status"] == "draft"

    out = await _caller_says(voice, "Yes, 3 o'clock is great.", operation="schedule", **proposal)

    assert out["status"] != "already_booked"
    assert _square_starts(world) == ["2026-10-22T19:00:00Z"], "nothing written before the confirmation"
    result = await _caller_confirms(world, voice)
    assert result.outcome is BookingOutcome.RESCHEDULED
    assert _square_starts(world) == ["2026-10-22T20:00:00Z"]


async def test_answer_change_it_then_moves_the_appointment(world):
    voice, row = await _voice_after_booking(world)
    proposal = {"requested_start_iso": local(THU_3PM_UTC), "service_description": "Swedish Massage"}
    asked = await _caller_says(voice, "Actually, can we do 3?", **proposal)
    assert asked["status"] == "needs_clarification"

    out = await _caller_says(voice, "Change the existing one, please.", **proposal)
    assert out["status"] == "draft" and out["operation"] == "reschedule"
    assert BOOKING_CHANGE_CLARIFICATION not in voice.session.entities

    result = await _caller_confirms(world, voice)
    assert result.outcome is BookingOutcome.RESCHEDULED
    assert _square_starts(world) == ["2026-10-22T20:00:00Z"]
    assert len(world.square.creates) == 1


async def test_answer_add_another_then_books_a_separate_appointment(world):
    voice, row = await _voice_after_booking(world)
    proposal = {"requested_start_iso": local(THU_3PM_UTC + HOUR), "service_description": "Signature Facial",
                "caller_name": "Ada Lovelace"}
    asked = await _caller_says(voice, "Actually, can we do a facial at 4?", **proposal)
    assert asked["status"] == "needs_clarification"

    out = await _caller_says(voice, "Keep it and book an additional one.", **proposal)
    assert out["status"] == "draft" and out["operation"] == "schedule"
    assert get_draft(voice.session).appointment_id is None

    result = await _caller_confirms(world, voice)
    assert result.outcome is BookingOutcome.BOOKED, result.message
    assert _square_starts(world) == ["2026-10-22T19:00:00Z", "2026-10-22T21:00:00Z"]
    assert row.start_time == THU_2PM_UTC


async def test_model_says_reschedule_but_caller_says_another_is_asked(world):
    voice, _row = await _voice_after_booking(world)
    out = await _caller_says(voice, "Can I book another massage at 3?",
                             operation="reschedule",
                             requested_start_iso=local(THU_3PM_UTC),
                             service_description="Swedish Massage")
    assert out["status"] == "needs_clarification"
    assert world.square.updates == [] and len(world.square.creates) == 1


# --------------------------------------------------------------------------- #
# Repeats and retries never duplicate
# --------------------------------------------------------------------------- #
async def test_restating_the_booked_details_creates_nothing(world, monkeypatch):
    voice, _row = await _voice_after_booking(world)
    monkeypatch.setattr(xai_realtime, "stage_booking",
                        lambda *a, **k: pytest.fail("a restatement must not stage anything"))
    booking_id = get_draft(voice.session).booking_id

    out = await _caller_says(voice, "Great, so that's 2pm Thursday for the Swedish massage?",
                             operation="schedule",
                             requested_start_iso=local(THU_2PM_UTC),
                             service_description="Swedish Massage")

    assert out["status"] == "already_booked" and out["booked"] is False
    assert get_draft(voice.session).booking_id == booking_id
    assert len(world.square.creates) == 1


async def test_identical_second_booking_in_one_call_is_refused_by_the_engine(world):
    voice, row = await _voice_after_booking(world)
    # Even if a request is misrouted into a new intent, the engine refuses a
    # second identical appointment for the same person in the same call.
    start_new_intent(voice.session)
    staged = await stage_booking(world.db, voice.session, AppointmentIntent(
        intent="schedule", confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(THU_2PM_UTC), service_description="Swedish Massage",
    ))
    assert staged.outcome is BookingOutcome.DRAFT  # Bea is free at 2pm too
    result = await _caller_confirms(world, voice)

    assert result.outcome is BookingOutcome.MISSING_INFO
    assert "Nothing new was booked" in result.message
    assert len(world.square.creates) == 1 and world.store.live == [row]


async def test_same_slot_for_a_named_guest_is_a_real_second_booking(world):
    voice, row = await _voice_after_booking(world)
    out = await _caller_says(voice, "Can I also book the same for my wife, Grace?",
                             requested_start_iso=local(THU_2PM_UTC),
                             service_description="Swedish Massage",
                             caller_name="Ada Lovelace",
                             guest_name="Grace Hopper")
    assert out["status"] == "draft", out

    result = await _caller_confirms(world, voice)
    assert result.outcome is BookingOutcome.BOOKED, result.message
    assert len(world.square.creates) == 2
    teams = sorted(b["appointment_segments"][0]["team_member_id"] for b in world.square.active())
    assert teams == ["TM_ANA", "TM_BEA"]


# --------------------------------------------------------------------------- #
# The start_new_appointment tool is held to the same rule
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("utterance,status,new_intent", [
    ("Actually, can we do 3 o'clock instead?", "needs_clarification", False),
    ("Actually, can we do 3?", "needs_clarification", False),
    ("I'd like to book a second appointment.", "new_intent", True),
])
async def test_start_new_appointment_tool_follows_the_callers_words(world, utterance, status, new_intent):
    voice, row = await _voice_after_booking(world)
    voice._user_turn_count += 1
    voice.session.add_turn("user", utterance)

    out = json.loads(await voice._run_start_new_appointment())

    assert out["status"] == status
    draft = get_draft(voice.session)
    assert (draft.appointment_id is None) is new_intent
    if not new_intent:
        assert draft.appointment_id == str(row.id)
