"""Brick 1 repair: a booking made on a call is never changed without fresh,
explicit authorization, on either voice path.

Permanent regressions for every failure found by the independent Brick 1
verification (P0 "never mind" then a later "yes" moving the booking; P1
unspoken questions, lost state on reconnect, the unguarded legacy loop, the
additional-appointment flow and false failure lines; P2 phrase recognition).

Everything here is SIMULATED: the real realtime dispatcher, the real legacy
<Gather> handler and the real booking engine run against the stateful
in-memory FakeSquare (tests/square_world.py). No real Square, no production.
"""
from __future__ import annotations

import itertools
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services import booking_change_guard as guard
from app.services.appointment_booking_service import BookingOutcome, confirm_booking, stage_booking
from app.services.booking_conversation import offer_accepted
from app.services.booking_state import (
    arm_verified_proposal,
    classify_change_question_answer,
    get_draft,
    mark_read_back,
    record_pure_confirmation,
)
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests import square_world
from tests.square_world import THU_2PM_UTC, call, local, square_spa, voice_for, wire

HOUR = timedelta(hours=1)
T2, T3, T4, T5 = (THU_2PM_UTC + n * HOUR for n in range(4))
Z2, Z3, Z4, Z5 = (t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in (T2, T3, T4, T5))
# The caller speaks on the Tuesday before Thursday 22 October 2026.
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=ZoneInfo(square_world.CHICAGO))
_ids = itertools.count(1)


# --------------------------------------------------------------------------- #
# Simulated worlds: a spa menu and representative non-spa businesses
# --------------------------------------------------------------------------- #
BUSINESSES = {
    "spa": [
        {"name": "Swedish Massage", "duration_minutes": 60,
         "square_variation_id": "var_swedish_60", "square_variation_version": 3},
        {"name": "Signature Facial", "duration_minutes": 60,
         "square_variation_id": "var_facial_60", "square_variation_version": 5},
    ],
    "hvac": [
        {"name": "AC Tune-Up", "duration_minutes": 60,
         "square_variation_id": "var_ac_tuneup", "square_variation_version": 1},
        {"name": "Furnace Inspection", "duration_minutes": 60,
         "square_variation_id": "var_furnace", "square_variation_version": 1},
    ],
    "dental": [
        {"name": "Teeth Cleaning", "duration_minutes": 60,
         "square_variation_id": "var_cleaning", "square_variation_version": 2},
        {"name": "Dental Exam", "duration_minutes": 60,
         "square_variation_id": "var_exam", "square_variation_version": 2},
    ],
}


def make_world(monkeypatch, business="spa"):
    menu = BUSINESSES[business]
    for item in menu:
        monkeypatch.setitem(square_world.VERSIONS, item["square_variation_id"], item["square_variation_version"])
        monkeypatch.setitem(square_world.MINUTES, item["square_variation_id"], item["duration_minutes"])
    w = wire(monkeypatch, spa=square_spa(services=menu))
    for start in (T2, T3, T4, T5):
        for team in ("TM_ANA", "TM_BEA"):
            for item in menu:
                w.square.open(start, item["square_variation_id"], team)
    w.monkeypatch = monkeypatch
    w.first, w.second = menu[0]["name"], menu[1]["name"]
    w.first_var, w.second_var = menu[0]["square_variation_id"], menu[1]["square_variation_id"]
    return w


class Phone:
    """One simulated call through the real realtime handler."""

    def __init__(self, world, session: CallSession | None = None):
        self.world = world
        self.session = session or call(world.spa)
        self.voice = voice_for(world, self.session, world.monkeypatch)
        self.voice._now_provider = lambda tz: NOW.astimezone(tz)
        self.heard: list[str] = []
        self.outputs: list[dict] = []
        self.voice._send = self._capture

        async def _no_consultation(*_a, **_k):
            return False  # the spa consultation script is not part of these rules
        self.voice._enforce_service_consultation = _no_consultation

    async def _capture(self, payload):
        item = payload.get("item") or {}
        if item.get("type") == "force_message":
            self.heard.append(item["content"][0]["text"])
        elif item.get("type") == "function_call_output":
            self.outputs.append(json.loads(item["output"]))

    async def says(self, text: str) -> list[str]:
        before = len(self.heard)
        await self.voice._dispatch({
            "type": "conversation.item.input_audio_transcription.completed", "transcript": text,
        })
        return self.heard[before:]

    async def tool(self, name: str, **args) -> dict:
        before = len(self.outputs)
        await self.voice._handle_function_call(
            {"name": name, "call_id": f"call_{next(_ids)}", "arguments": json.dumps(args)}
        )
        return self.outputs[-1] if len(self.outputs) > before else {}

    async def propose(self, **args) -> dict:
        return await self.tool("propose_appointment", **args)

    def starts(self) -> list[str]:
        return sorted(b["start_at"] for b in self.world.square.active())

    def variations(self) -> list[str]:
        return [b["appointment_segments"][0]["service_variation_id"] for b in self.world.square.active()]


async def booked_call(world) -> Phone:
    """The caller books Thursday 2 PM end to end through the realtime path."""
    p = Phone(world)
    p.voice._pending_caller = f"I'd like a {world.first} on Thursday at 2 PM. My name is Ada Lovelace."
    p.voice._flush_caller_turn()
    out = await p.propose(requested_start_iso=local(T2), service_description=world.first,
                          caller_name="Ada Lovelace", operation="schedule")
    assert out["status"] == "draft", out
    await p.says("Yes, please.")
    assert p.starts() == [Z2], p.heard
    assert len(world.square.creates) == 1
    return p


def reconnect(p: Phone) -> Phone:
    """Media-stream reconnect: the session comes back from Redis JSON into a
    brand-new realtime session object."""
    restored = CallSession.from_dict(json.loads(json.dumps(p.session.to_dict())))
    q = Phone(p.world, restored)
    return q


def only_the_original(p: Phone):
    assert p.starts() == [Z2], p.world.square.active()
    assert len(p.world.square.creates) == 1, "an extra booking was created"
    assert p.world.square.updates == [], "the booking was modified"


async def every_kind_of_yes(p: Phone):
    """Yes through the transcript path, the model's confirm tool, and the engine."""
    await p.says("Yes.")
    await p.tool("confirm_appointment")
    await p.says("Yes, book it.")
    await p.tool("confirm_appointment")
    arm_verified_proposal(p.session)
    await confirm_booking(p.world.db, p.session)


THREE = lambda w: dict(requested_start_iso=local(T3), service_description=w.first, caller_name="Ada Lovelace")  # noqa: E731


# --------------------------------------------------------------------------- #
# P0: "never mind" / "keep it" while a change is pending
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("business", sorted(BUSINESSES))
async def test_never_mind_then_unrelated_yes_never_moves_the_booking(monkeypatch, business):
    w = make_world(monkeypatch, business)
    p = await booked_call(w)
    await p.says("Can we move it to Thursday at 3 PM instead?")
    staged = await p.propose(operation="reschedule", **THREE(w))
    assert staged["status"] == "draft" and staged["operation"] == "reschedule"

    heard = await p.says("Never mind, keep it.")  # the model does not call a tool
    assert any("stays exactly as it is" in line for line in heard), heard
    p.session.add_turn("assistant", "Sure. Anything else I can help with?")
    await p.says("Yes.")
    await p.says("Yes, where are you located?")

    only_the_original(p)
    assert p.session.booking_status == "booked"
    assert not offer_accepted(p.session)


@pytest.mark.parametrize("model_call", ["resend_change", "start_new", "confirm", "none"])
async def test_never_mind_after_staged_change_whatever_the_model_does(monkeypatch, model_call):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says("Can we move it to Thursday at 3 PM instead?")
    await p.propose(operation="reschedule", **THREE(w))
    await p.says("Never mind.")
    if model_call == "resend_change":
        out = await p.propose(operation="reschedule", **THREE(w))
        assert out["status"] == "kept_existing" and out["booked"] is False
    elif model_call == "start_new":
        assert (await p.tool("start_new_appointment"))["status"] == "kept_existing"
    elif model_call == "confirm":
        assert (await p.tool("confirm_appointment"))["booked"] is False
    await every_kind_of_yes(p)
    only_the_original(p)


async def test_three_then_four_then_keep_two(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says("Can we move it to Thursday at 3 PM instead?")
    await p.propose(operation="reschedule", **THREE(w))
    await p.says("Actually make it 4 PM instead.")
    await p.propose(operation="reschedule", requested_start_iso=local(T4), service_description=w.first)
    heard = await p.says("No, never mind, leave it at 2.")
    assert any("Thursday at 2 PM stays exactly as it is" in line for line in heard), heard
    await every_kind_of_yes(p)
    only_the_original(p)


async def test_keep_restores_service_and_session_mirrors(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says(f"Can I switch it to a {w.second} instead, same time Thursday at 2 PM?")
    await p.propose(operation="reschedule", requested_start_iso=local(T2), service_description=w.second)
    await p.says(f"Never mind, keep the {w.first.split()[0].lower()}.")
    assert p.session.selected_service == w.first
    assert get_draft(p.session).service_description == w.first
    assert p.session.booking_status == "booked"
    await every_kind_of_yes(p)
    only_the_original(p)
    assert p.variations() == [w.first_var]


async def test_completed_booking_needs_fresh_explicit_authorization(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    # The consent that produced the booking is retired with it.
    assert not offer_accepted(p.session)
    for key in ("accepted_booking_offer", "spoken_booking_offer", "spoken_booking_choices"):
        assert key not in p.session.entities
    assert not get_draft(p.session).confirmation_authorized
    # A bare yes with nothing new read back changes nothing.
    await p.says("Yes.")
    only_the_original(p)
    # A change is written only after its own read-back and a yes on a later turn.
    await p.says("Can we move it to Thursday at 3 PM instead?")
    staged = await p.propose(operation="reschedule", **THREE(w))
    assert staged["status"] == "draft"
    assert w.square.updates == []
    await p.says("Yes, please.")
    assert p.starts() == [Z3] and len(w.square.updates) == 1 and len(w.square.creates) == 1


# --------------------------------------------------------------------------- #
# P1: required questions and confirmations are actually spoken
# --------------------------------------------------------------------------- #
async def test_change_question_and_keep_line_are_spoken(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says("Actually, can we do Thursday at 3 PM?")
    before = len(p.heard)
    asked = await p.propose(**THREE(w))
    assert asked["status"] == "needs_clarification"
    assert any("keep everything as it is" in line for line in p.heard[before:]), p.heard[before:]

    before = len(p.heard)
    await p.says("The new time.")
    again = await p.propose(**THREE(w))
    assert again["status"] == "needs_clarification"
    assert any("keep it as is" in line for line in p.heard[before:]), p.heard[before:]
    assert not any(re.search(r"is available|would you like me to book", line, re.I) for line in p.heard[before:])

    heard = await p.says("Keep it.")
    assert any("stays exactly as it is" in line for line in heard), heard
    out = await p.propose(**THREE(w))  # the model re-sends the change anyway
    assert out["status"] == "kept_existing"
    assert "Thursday at 2 PM" in out["spoken"]
    await every_kind_of_yes(p)
    only_the_original(p)


async def test_keep_names_the_latest_booking(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says("Can we move it to Thursday at 3 PM?")
    await p.propose(operation="reschedule", **THREE(w))
    await p.says("Yes.")
    assert p.starts() == [Z3]
    await p.says("Hmm, what about Thursday at 4 PM?")
    await p.propose(requested_start_iso=local(T4), service_description=w.first)
    heard = await p.says("No, never mind, keep it.")
    assert any("Thursday at 3 PM stays exactly as it is" in line for line in heard), heard
    await every_kind_of_yes(p)
    assert p.starts() == [Z3] and len(w.square.creates) == 1 and len(w.square.updates) == 1


# --------------------------------------------------------------------------- #
# P1: reconnects
# --------------------------------------------------------------------------- #
async def test_turn_counters_survive_a_reconnect(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    turns, done = p.voice._user_turn_count, p.voice._booking_completed_turn
    q = reconnect(p)
    assert (q.voice._user_turn_count, q.voice._booking_completed_turn) == (turns, done)


@pytest.mark.parametrize("point", ["after_question", "after_staged_change"])
async def test_keep_after_reconnect(monkeypatch, point):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    if point == "after_question":
        await p.says("Can we do Thursday at 3 PM?")
        assert (await p.propose(**THREE(w)))["status"] == "needs_clarification"
    else:
        await p.says("Move it to Thursday at 3 PM please.")
        assert (await p.propose(operation="reschedule", **THREE(w)))["status"] == "draft"
    q = reconnect(p)
    heard = await q.says("Never mind, keep it.")
    assert any("stays exactly as it is" in line for line in heard), heard
    assert (await q.propose(**THREE(w)))["status"] == "kept_existing"
    await every_kind_of_yes(q)
    only_the_original(q)


async def test_additional_booking_after_reconnect_books_once(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    for _ in range(3):
        await p.says("Okay.")
    await p.says(f"Can I book another appointment, a {w.second} Thursday at 4 PM?")
    out = await p.propose(operation="schedule", requested_start_iso=local(T4),
                          service_description=w.second, caller_name="Ada Lovelace")
    assert "this would be an additional appointment" in out["spoken"]
    q = reconnect(p)
    heard = await q.says("Yes, please.")
    assert not any("wasn't able to complete" in line for line in heard), heard
    assert q.starts() == [Z2, Z4] and len(w.square.creates) == 2 and w.square.updates == []
    await every_kind_of_yes(q)
    assert len(w.square.creates) == 2


# --------------------------------------------------------------------------- #
# P1: additional appointments and false failure lines
# --------------------------------------------------------------------------- #
async def test_additional_with_taken_time_then_alternative_books_without_false_failure(monkeypatch):
    w = make_world(monkeypatch)
    w.square.openings = [o for o in w.square.openings
                         if not (o["variation"] == w.second_var and o["start"] == T4)]
    p = await booked_call(w)
    await p.says(f"I'd like to book another appointment, a {w.second} on Thursday at 4 PM.")
    out = await p.propose(operation="schedule", requested_start_iso=local(T4),
                          service_description=w.second, caller_name="Ada Lovelace")
    assert out["status"] == "conflict"
    assert "this would be an additional appointment" in p.heard[-1]
    heard = await p.says("5 PM works.")
    assert not any("wasn't able to complete" in line for line in heard), heard
    assert p.starts() == [Z2, Z5] and len(w.square.creates) == 2 and w.square.updates == []


async def test_offered_alternative_on_first_booking_books_without_false_failure(monkeypatch):
    w = make_world(monkeypatch)
    w.square.openings = [o for o in w.square.openings
                         if not (o["variation"] == w.second_var and o["start"] == T4)]
    p = Phone(w)
    p.voice._pending_caller = f"A {w.second} Thursday at 4 PM please, I'm Ada Lovelace."
    p.voice._flush_caller_turn()
    out = await p.propose(requested_start_iso=local(T4), service_description=w.second,
                          caller_name="Ada Lovelace", operation="schedule")
    assert out["status"] == "conflict"
    heard = await p.says("5 PM works.")
    assert not any("wasn't able to complete" in line for line in heard), heard
    assert p.starts() == [Z5] and len(w.square.creates) == 1


async def test_backend_yes_speaks_the_additional_notice_not_a_failure(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says("I'd like to book a second appointment.")
    assert (await p.tool("start_new_appointment"))["status"] == "new_intent"
    # The new request is staged by a path that never spoke the notice.
    staged = await stage_booking(w.db, p.session, AppointmentIntent(
        intent="schedule", confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(T4), service_description=w.second))
    assert staged.outcome is BookingOutcome.DRAFT
    arm_verified_proposal(p.session)
    p.session.booking_status = "awaiting_confirmation"
    heard = await p.says("Yes.")
    assert heard and "this would be an additional appointment" in heard[-1], heard
    assert not any("wasn't able to complete" in line for line in heard)
    assert len(w.square.creates) == 1, "nothing may be booked before the caller answers"
    await p.says("Yes, please book it.")
    assert p.starts() == [Z2, Z4] and len(w.square.creates) == 2


async def test_caller_declines_the_additional_appointment(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says(f"Can I also book a {w.second} Thursday at 4 PM?")
    await p.propose(operation="schedule", requested_start_iso=local(T4),
                    service_description=w.second, caller_name="Ada Lovelace")
    heard = await p.says("Never mind, keep it.")
    assert any("stays exactly as it is" in line for line in heard), heard
    await every_kind_of_yes(p)
    only_the_original(p)


# --------------------------------------------------------------------------- #
# P2: phrase recognition
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("phrase,expected", [
    ("Keep it.", "keep"), ("Never mind.", "keep"), ("Leave it as it is.", "keep"),
    ("No, keep the original.", "keep"), ("Just keep it.", "keep"), ("Keep my appointment.", "keep"),
    ("Don't change it.", "keep"), ("déjalo así", "keep"),
    ("Keep me posted.", "ambiguous"), ("I'll keep that in mind.", "ambiguous"),
    ("Keep in touch.", "ambiguous"), ("the new one", "ambiguous"), ("that one", "ambiguous"),
    ("otra cita", "ambiguous"), ("yes", "ambiguous"),
    ("add a new one", "additional"), ("keep it and add one", "additional"), ("both", "additional"),
    ("change it", "modify"), ("the first one", "modify"),
])
def test_change_question_answer_phrases(phrase, expected):
    assert classify_change_question_answer(phrase) == expected


@pytest.mark.parametrize("phrase", [
    "keep it at 3", "keep me posted", "I'll keep that in mind", "the new one", "that one",
    "the 3 PM one", "yes", "okay",
])
async def test_unclear_answers_are_asked_again_and_write_nothing(monkeypatch, phrase):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says("Actually, can we do Thursday at 3 PM?")
    assert (await p.propose(**THREE(w)))["status"] == "needs_clarification"
    await p.says(phrase)
    out = await p.propose(**THREE(w))
    assert out["status"] == "needs_clarification", (phrase, out)
    await p.says("Yes.")
    await p.tool("confirm_appointment")
    only_the_original(p)


def test_keep_with_a_different_time_is_not_a_keep():
    session = call(SimpleNamespace(id="00000000-0000-0000-0000-000000000001"))
    session.entities[guard.BOOKED_DRAFT_SNAPSHOT] = {"draft": {"start_iso": local(T2), "selected_slot": {"start": Z2}}}
    tz = ZoneInfo(square_world.CHICAGO)
    assert guard.keep_request(session, "keep it", tz)
    assert guard.keep_request(session, "keep it at 2", tz)
    assert guard.keep_request(session, "leave it at 2 PM", tz)
    assert not guard.keep_request(session, "keep it at 3", tz)
    assert not guard.keep_request(session, "keep it but on Friday", tz)


# --------------------------------------------------------------------------- #
# Engine-level backstop (any path, any model output)
# --------------------------------------------------------------------------- #
async def test_engine_stages_nothing_after_keep_whatever_the_model_extracted(monkeypatch):
    w = make_world(monkeypatch)
    session = call(w.spa)
    session.add_turn("user", f"A {w.first} Thursday at 2 PM, I'm Ada Lovelace.")
    assert (await stage_booking(w.db, session, AppointmentIntent(
        intent="schedule", confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(T2), service_description=w.first))).outcome is BookingOutcome.DRAFT
    arm_verified_proposal(session)
    session.add_turn("user", "Yes.")
    assert (await confirm_booking(w.db, session)).outcome is BookingOutcome.BOOKED

    session.add_turn("user", "Keep it.")
    staged = await stage_booking(w.db, session, AppointmentIntent(
        intent="schedule", confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(T3), service_description=w.first))
    assert staged.outcome is BookingOutcome.MISSING_INFO
    assert "keep their existing appointment" in staged.message
    mark_read_back(session)
    session.add_turn("user", "Yes.")
    if record_pure_confirmation(session, "Yes."):
        await confirm_booking(w.db, session)
    assert [b["start_at"] for b in w.square.active()] == [Z2]
    assert len(w.square.creates) == 1 and w.square.updates == []


# --------------------------------------------------------------------------- #
# Legacy <Gather> text loop
# --------------------------------------------------------------------------- #
class LegacyCall:
    """Drives the real telephony.voice_respond with scripted model outputs."""

    def __init__(self, world, monkeypatch):
        from app.api.v1 import telephony

        self.world = world
        self.telephony = telephony
        self.session = call(world.spa, sid="CA-legacy")
        self.replies: list[str] = []
        self.next_intent: AppointmentIntent | None = None
        self.next_reply = "Okay."

        async def _extract(_session):
            return self.next_intent

        async def _reply(_session):
            return self.next_reply

        async def _noop(*_a, **_k):
            return None

        monkeypatch.setattr(telephony.grok_service, "extract_appointment_intent", _extract)
        monkeypatch.setattr(telephony.grok_service, "generate_voice_response", _reply)
        monkeypatch.setattr(telephony, "persist_caller_identity", _noop)
        outer = self

        class _State:
            async def get(self, _sid):
                return outer.session

            async def save(self, session):
                # Every turn goes through Redis JSON, as in production.
                outer.session = CallSession.from_dict(json.loads(json.dumps(session.to_dict())))

        self.state = _State()

    async def says(self, speech, *, intent=None, reply="Okay."):
        self.next_intent, self.next_reply = intent, reply
        await self.telephony.voice_respond(
            CallSid=self.session.call_sid, SpeechResult=speech, db=self.world.db, state=self.state,
        )
        said = self.session.history[-1]["content"]
        self.replies.append(said)
        return said

    def starts(self):
        return sorted(b["start_at"] for b in self.world.square.active())


def _intent(kind, start, service):
    return AppointmentIntent(intent=kind, confidence=1.0, caller_name="Ada Lovelace",
                             requested_start_iso=local(start), service_description=service)


async def _legacy_booked(world, monkeypatch) -> LegacyCall:
    c = LegacyCall(world, monkeypatch)
    await c.says(f"I'd like a {world.first} Thursday at 2 PM, I'm Ada Lovelace.",
                 intent=_intent("schedule", T2, world.first),
                 reply="Thursday at 2 PM is open. Would you like me to book that?")
    await c.says("Yes.", reply="You're all set. Is there anything else I can help you with today?")
    assert c.starts() == [Z2] and len(world.square.creates) == 1
    return c


@pytest.mark.parametrize("business", sorted(BUSINESSES))
async def test_legacy_ambiguous_request_is_asked_then_keep_writes_nothing(monkeypatch, business):
    w = make_world(monkeypatch, business)
    c = await _legacy_booked(w, monkeypatch)
    said = await c.says("Actually, can we do 3?", intent=_intent("schedule", T3, w.first),
                        reply="3 PM is available. Would you like me to book that?")
    assert "keep everything as it is" in said, said
    said = await c.says("Keep it.", intent=_intent("schedule", T3, w.first),
                        reply="3 PM is available. Would you like me to book that?")
    assert "stays exactly as it is" in said, said
    await c.says("Yes.", reply="Great.")
    await c.says("Yes, please book it.", reply="Great.")
    assert c.starts() == [Z2] and len(w.square.creates) == 1 and w.square.updates == []


async def test_legacy_never_mind_after_staged_change_then_yes(monkeypatch):
    w = make_world(monkeypatch)
    c = await _legacy_booked(w, monkeypatch)
    await c.says("Can you move it to 3 instead?", intent=_intent("reschedule", T3, w.first),
                 reply="3 PM is available. Would you like me to move it?")
    said = await c.says("Never mind, keep it.", intent=_intent("reschedule", T3, w.first))
    assert "stays exactly as it is" in said
    await c.says("Yes.", reply="Great.")
    assert c.starts() == [Z2] and w.square.updates == [] and len(w.square.creates) == 1


async def test_legacy_explicit_reschedule_still_works(monkeypatch):
    w = make_world(monkeypatch)
    c = await _legacy_booked(w, monkeypatch)
    await c.says("Can you move it to 3 instead?", intent=_intent("reschedule", T3, w.first),
                 reply="3 PM is available. Would you like me to move it?")
    assert w.square.updates == []
    await c.says("Yes.", reply="Done.")
    assert c.starts() == [Z3] and len(w.square.updates) == 1 and len(w.square.creates) == 1


async def test_legacy_additional_is_announced_before_it_is_booked(monkeypatch):
    w = make_world(monkeypatch)
    c = await _legacy_booked(w, monkeypatch)
    said = await c.says(f"I'd also like to book another appointment, a {w.second} Thursday at 4.",
                        intent=_intent("schedule", T4, w.second),
                        reply="4 PM is available. Would you like me to book that?")
    assert said.startswith("Just to be clear, this would be an additional appointment."), said
    assert len(w.square.creates) == 1
    await c.says("Yes.", reply="Done.")
    assert c.starts() == [Z2, Z4] and len(w.square.creates) == 2 and w.square.updates == []


# --------------------------------------------------------------------------- #
# Tenant isolation and universal compatibility
# --------------------------------------------------------------------------- #
async def test_one_calls_booking_state_never_affects_another_call(monkeypatch):
    w = make_world(monkeypatch)
    a = await booked_call(w)
    await a.says("Can we move it to Thursday at 3 PM instead?")
    await a.propose(operation="reschedule", **THREE(w))
    a_entities = json.dumps(a.session.entities, sort_keys=True, default=str)

    other_tenant = square_spa(services=BUSINESSES["spa"])
    b = Phone(w, call(other_tenant, sid="CA-other"))
    await b.says("Never mind, keep it.")
    assert not guard.booking_on_record(b.session)
    assert b.heard == []
    assert json.dumps(a.session.entities, sort_keys=True, default=str) == a_entities
    assert w.square.updates == []


def test_shared_guard_has_no_industry_or_tenant_specific_terms():
    source = Path(guard.__file__).read_text().casefold()
    for term in ("spa", "massage", "facial", "ruby", "salon", "therapist", "square"):
        assert not re.search(rf"\b{term}\b", source), term
