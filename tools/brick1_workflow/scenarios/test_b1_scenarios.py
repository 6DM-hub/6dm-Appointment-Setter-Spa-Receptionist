"""Independent Brick 1 scenario tests. SIMULATED: in-memory fake Square
(tests/square_world.FakeSquare behind the real SquareAdapter), real realtime
dispatcher + handlers + booking engine. Not a real Square integration test.

Every scenario ends by inspecting the fake Square's stored bookings and the
local appointment rows.
"""
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import json

import pytest

from harness import (
    T2, T3, T4, T5, Z2, Z3, Z4, Z5, Phone, book_2pm, engine_yes, local, make_world,
)
from app.services.appointment_booking_service import BookingOutcome
from app.services.booking_state import get_draft
from app.services.call_state import CallSession

SW = "Swedish Massage"
THREE = dict(requested_start_iso=local(T3), service_description=SW, caller_name="Ada Lovelace")
FOUR = dict(requested_start_iso=local(T4), service_description=SW)
TWO = dict(requested_start_iso=local(T2), service_description=SW)
def note(tag, ok, detail=""):
    with open(__import__("os").path.join(__import__("os").environ.get("B1_LOG_DIR", __import__("os").path.dirname(__import__("os").path.abspath(__file__))), "notes.log"), "a") as fh:
        fh.write(f"{tag}\t{'OK' if ok else 'MISS'}\t{detail}\n")


OFFER_WORDS = ("is available", "would you like me to book", "would you like me to move")


def only_original(p, *, start=Z2):
    """Square holds exactly the original booking, unmodified."""
    sq = p.square()
    import os
    note(f"STATE {os.environ.get('PYTEST_CURRENT_TEST','').split(' ')[0].split('::')[-1]}", True,
         f"creates={len(p.world.square.creates)} puts={len(p.world.square.updates)} "
         f"active={[(b['start'][11:16], b['variation'], b['team'], b['version']) for b in sq]} rows={len(p.rows())}")
    assert len(p.world.square.creates) == 1, ("extra Square create", sq)
    assert p.world.square.updates == [], ("Square booking was modified", p.world.square.updates)
    assert [b["start"] for b in sq] == [start], sq
    assert sq[0]["variation"] == "var_swedish_60" and sq[0]["version"] == 1, sq
    assert len(p.rows()) == 1, p.rows()
    p.json_roundtrip_ok()


def no_new_offer_since(p, idx):
    for line in p.heard[idx:]:
        low = line.lower()
        assert not any(w in low for w in OFFER_WORDS), ("CARA proposed a slot", line)


async def yes_and_name(p, yes="Yes, please."):
    """Caller says yes; if CARA asks for the name again (new intent), give it."""
    await p.says(yes)
    if "Can I get your name" in p.last_heard():
        await p.says("Ada Lovelace.")


async def everyone_says_yes(p):
    """The caller says 'yes' to whatever CARA last read back: through the
    backend transcript path, through the model's confirm tool, and straight
    into confirm_booking."""
    await p.says("Yes.")
    await p.confirm()
    await p.says("Yes, book it.")
    await p.confirm()
    return await engine_yes(p)


# --------------------------------------------------------------------------- #
# 1. "Keep it"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model_call", ["resend_3pm", "resend_3pm_schedule", "start_new", "no_tool"])
async def test_s1_keep_it(monkeypatch, model_call):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Actually, can we do Thursday at 3 PM?")
    asked = await p.propose(**THREE)
    note(f"s1[{model_call}] asked", asked["status"] == "needs_clarification", asked.get("status"))
    note(f"s1[{model_call}] question audible", "keep everything as it is" in p.last_heard(), p.last_heard())
    idx = len(p.heard)
    await p.says("Keep it.")
    if model_call == "resend_3pm":
        out = await p.propose(**THREE)
    elif model_call == "resend_3pm_schedule":
        out = await p.propose(operation="schedule", **THREE)
    elif model_call == "start_new":
        out = await p.start_new()
    else:
        out = {"status": "no_tool"}
    if model_call != "no_tool":
        note(f"s1[{model_call}] status", out["status"] == "kept_existing", out.get("status"))
    note(f"s1[{model_call}] kept speech audible", any("stays exactly as it is" in h for h in p.heard[idx:]), p.heard[idx:])
    await everyone_says_yes(p)
    try:
        only_original(p)
    finally:
        no_new_offer_since(p, idx)


# --------------------------------------------------------------------------- #
# 2. "Never mind" - including a change already staged and read back
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model_call", ["resend_3pm", "start_new", "no_tool", "confirm_wrongly"])
async def test_s2_never_mind_after_staged_change(monkeypatch, model_call):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can we move it to Thursday at 3 PM instead?")
    staged = await p.propose(operation="reschedule", **THREE)
    assert staged["status"] == "draft" and staged["operation"] == "reschedule", staged
    note(f"s2[{model_call}] 3PM read back", "3 PM" in p.last_heard(), p.last_heard())
    idx = len(p.heard)
    await p.says("Never mind.")
    if model_call == "resend_3pm":
        out = await p.propose(operation="reschedule", **THREE)
        note(f"s2[{model_call}] status", out["status"] == "kept_existing", out.get("status"))
    elif model_call == "start_new":
        out = await p.start_new()
        note(f"s2[{model_call}] status", out["status"] == "kept_existing", out.get("status"))
    elif model_call == "confirm_wrongly":
        out = await p.confirm()
        assert out.get("booked") is not True or out.get("status") == "booked"
    # CARA (model) says "OK, it stays at 2. Anything else?" -> caller "Yes."
    await everyone_says_yes(p)
    try:
        only_original(p)
    finally:
        no_new_offer_since(p, idx)


async def test_s2b_never_mind_without_any_staged_change(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Could I do Thursday at 3 PM?")
    await p.propose(**THREE)
    await p.says("Never mind.")
    out = await p.propose(**THREE)
    note("s2b status", out["status"] == "kept_existing", (out, p.heard[-1:]))
    await everyone_says_yes(p)
    only_original(p)


# --------------------------------------------------------------------------- #
# 3. "Leave everything the way it is" - no Square writes at all
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("staged_first", [False, True])
async def test_s3_leave_everything(monkeypatch, staged_first):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    writes_before = len(w.square.writes("POST")) + len(w.square.writes("PUT"))
    if staged_first:
        await p.says("Move it to Thursday at 3 PM please.")
        assert (await p.propose(operation="reschedule", **THREE))["status"] == "draft"
    else:
        await p.says("Hmm, what about Thursday at 3 PM?")
        assert (await p.propose(**THREE))["status"] == "needs_clarification"
    await p.says("Leave everything the way it is.")
    out = await p.propose(**THREE)
    note(f"s3[{staged_first}] status", out["status"] == "kept_existing", out.get("status"))
    await everyone_says_yes(p)
    assert len(w.square.writes("POST")) + len(w.square.writes("PUT")) == writes_before
    only_original(p)


# --------------------------------------------------------------------------- #
# 4. "The new time" - clarified, not assumed
# --------------------------------------------------------------------------- #
async def test_s4_the_new_time_is_clarified(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Actually, can we do Thursday at 3 PM?")
    await p.propose(**THREE)
    idx = len(p.heard)
    await p.says("The new time.")
    out = await p.propose(**THREE)
    note("s4 status", out["status"] == "needs_clarification", out.get("status"))
    note("s4 re-ask audible", any("keep it as is" in h for h in p.heard[idx:]), p.heard[idx:])
    await everyone_says_yes(p)
    try:
        only_original(p)
    finally:
        no_new_offer_since(p, idx)


# --------------------------------------------------------------------------- #
# 5. "Book another appointment"
# --------------------------------------------------------------------------- #
async def test_s5_additional_is_announced_then_booked_once(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("I'd also like to book another appointment, a Signature Facial on Thursday at 4 PM.")
    out = await p.propose(operation="schedule", requested_start_iso=local(T4),
                          service_description="Signature Facial")
    assert out["status"] == "draft", out
    said = p.last_heard()
    assert "additional appointment" in said and "Swedish Massage on Thursday at 2 PM stays booked" in said, said
    assert said.index("additional appointment") < said.lower().index("would you like me to book")
    # wrong model: confirms in the same breath, before the caller answered
    early = await p.confirm()
    assert early.get("booked") is False, early
    assert len(w.square.creates) == 1
    await yes_and_name(p)
    assert sorted(p.starts()) == [Z2, Z4], (p.square(), p.heard[-3:])
    # repeated yes / confirms create nothing more
    await p.confirm(); await p.says("Yes."); await engine_yes(p)
    sq = p.square()
    assert len(w.square.creates) == 2 and w.square.updates == [], sq
    assert {b["variation"] for b in sq} == {"var_swedish_60", "var_facial_60"}
    assert all(b["customer"] == "cust_1" for b in sq)
    assert sq[0]["version"] == 1
    assert len(p.rows()) == 2
    p.json_roundtrip_ok()


async def test_s5b_additional_then_caller_declines(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can I book a second appointment, a facial Thursday at 4 PM?")
    assert (await p.propose(operation="schedule", requested_start_iso=local(T4),
                            service_description="Signature Facial"))["status"] == "draft"
    await p.says("Oh, no. Then never mind.")
    out = await p.confirm()           # confused model
    assert out.get("booked") is False, out
    await p.says("No thanks.")
    await p.confirm()
    only_original(p)


async def test_s5c_additional_offered_via_day_part_window_hears_notice_before_booking(monkeypatch):
    """Additional appointment whose times are read back by the day-part window
    path (not propose's spoken). The caller must still hear it is additional
    before anything is booked."""
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("I'd like to book another appointment too.")
    assert (await p.start_new())["status"] == "new_intent"
    await p.says("A Signature Facial on Thursday afternoon.")
    await p.propose(operation="schedule", service_description="Signature Facial", caller_name="Ada Lovelace")
    offered = p.last_heard()
    await p.says("4 PM works.")
    heard_before_booking = list(p.heard)
    if len(w.square.creates) == 1:
        await p.says("Yes.")
    booked_extra = len(w.square.creates) > 1
    if booked_extra:
        # find what was heard before the write
        transcript = " | ".join(p.heard)
        assert any("additional appointment" in h for h in p.heard[:-1]), (
            "additional booked without the caller hearing it is additional", offered, transcript)
    p.json_roundtrip_ok()
    print("HEARD:", p.heard)


# --------------------------------------------------------------------------- #
# 6. "And one for my husband too"
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("when,team_free", [(T3, None), (T2, "TM_ANA")])
async def test_s6_husband_second_appointment(monkeypatch, when, team_free):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    my = p.square()[0]
    t = "3 PM" if when == T3 else "2 PM"
    await p.says(f"And one for my husband too, a Swedish massage Thursday at {t}. His name is John Lovelace.")
    out = await p.propose(operation="schedule", requested_start_iso=local(when), service_description=SW,
                          guest_name="John Lovelace")
    assert out["status"] == "draft", (out, p.heard[-1:])
    assert "additional appointment" in p.last_heard(), p.last_heard()
    await yes_and_name(p, "Yes please.")
    sq = p.square()
    assert len(sq) == 2 and len(w.square.creates) == 2 and w.square.updates == [], sq
    mine = [b for b in sq if b["start"] == my["start"] and b["team"] == my["team"]]
    assert mine and mine[0]["version"] == 1, sq
    guest = [b for b in sq if b is not mine[0]][0]
    print("GUEST BOOKING", guest)
    assert "John" in (guest["note"] or ""), ("guest identity not recorded on Square booking", guest)
    rows = p.rows()
    print("ROWS", [(r.start_time, r.title, getattr(r, "guest_name", None), r.notes if hasattr(r, "notes") else None) for r in rows])
    assert len(rows) == 2
    p.json_roundtrip_ok()


# --------------------------------------------------------------------------- #
# 7. Mind changes during rescheduling
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("final_call", ["propose_2pm", "propose_stale_4pm", "no_tool", "start_new"])
async def test_s7_three_then_four_then_keep_two(monkeypatch, final_call):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can we move it to Thursday at 3 PM?")
    assert (await p.propose(operation="reschedule", **THREE))["status"] == "draft"
    await p.says("Actually no, Thursday at 4 PM.")
    o = await p.propose(**FOUR)
    assert o["status"] == "draft" and o["operation"] == "reschedule", o
    idx = len(p.heard)
    await p.says("Never mind, keep Thursday at 2 PM.")
    if final_call == "propose_2pm":
        out = await p.propose(**TWO)
    elif final_call == "propose_stale_4pm":
        out = await p.propose(operation="reschedule", **FOUR)
    elif final_call == "start_new":
        out = await p.start_new()
    else:
        out = {"status": "no_tool"}
    print("FINAL", out.get("status"), p.heard[idx:])
    await everyone_says_yes(p)
    only_original(p)


async def test_s7b_completed_reschedule_then_keep_names_new_time(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can we move it to Thursday at 3 PM?")
    assert (await p.propose(operation="reschedule", **THREE))["status"] == "draft"
    await p.says("Yes.")
    assert p.starts() == [Z3], p.square()
    await p.says("Hmm, what about Thursday at 4 PM?")
    asked = await p.propose(**FOUR)
    await p.says("No, never mind, keep it.")
    out = await p.propose(**FOUR)
    note("s7b status", out["status"] == "kept_existing", (out, p.heard[-1:]))
    note("s7b kept speech names 3 PM", "3 PM" in p.last_heard(), p.heard[-2:])
    await everyone_says_yes(p)
    assert p.starts() == [Z3] and len(w.square.creates) == 1 and len(w.square.updates) == 1
    assert len(p.rows()) == 1


# --------------------------------------------------------------------------- #
# 8. Repeated confirmations never duplicate
# --------------------------------------------------------------------------- #
async def test_s8_repeated_confirmations(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    for _ in range(3):
        await p.confirm()
        await p.says("Yes.")
        await engine_yes(p)
    only_original(p)
    await p.says("Also book a second one, Signature Facial Thursday at 4 PM.")
    await p.propose(operation="schedule", requested_start_iso=local(T4), service_description="Signature Facial")
    for _ in range(3):
        await p.confirm()
    await yes_and_name(p, "Yes.")
    for _ in range(3):
        await p.confirm()
        await p.says("Yes.")
        await engine_yes(p)
    assert sorted(p.starts()) == [Z2, Z4] and len(w.square.creates) == 2 and w.square.updates == []
    assert len(p.rows()) == 2


# --------------------------------------------------------------------------- #
# 9. Interruptions and retries
# --------------------------------------------------------------------------- #
async def test_s9a_duplicate_call_id_and_same_turn_duplicates(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can I book another appointment, a facial Thursday at 4 PM?")
    a = await p.tool_same_id("propose_appointment", "dup1", operation="schedule",
                             requested_start_iso=local(T4), service_description="Signature Facial")
    b = await p.tool_same_id("propose_appointment", "dup1", operation="schedule",
                             requested_start_iso=local(T4), service_description="Signature Facial")
    assert a == b
    await yes_and_name(p, "Yes.")
    c1 = await p.tool_same_id("confirm_appointment", "dupc")
    c2 = await p.tool_same_id("confirm_appointment", "dupc")
    await p.confirm(); await p.confirm()
    assert sorted(p.starts()) == [Z2, Z4] and len(w.square.creates) == 2, p.square()


async def test_s9b_lost_response_after_square_write_then_retry(monkeypatch):
    """Square stores the booking but the HTTP response is lost (timeout)."""
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can I book another appointment, a facial Thursday at 4 PM?")
    await p.propose(operation="schedule", requested_start_iso=local(T4), service_description="Signature Facial")
    real = w.square._create
    state = {"n": 0}

    def flaky(body):
        res = real(body)
        state["n"] += 1
        if state["n"] == 1:
            raise TimeoutError("response lost after write")
        return res
    w.square._create = flaky
    await yes_and_name(p, "Yes.")
    print("AFTER TIMEOUT", p.heard[-1:], p.square())
    await p.says("Yes.")
    await p.confirm()
    await engine_yes(p)
    print("AFTER RETRY", p.heard[-1:], p.square())
    assert len(w.square.active()) == 2, p.square()
    assert sorted(p.starts()) == [Z2, Z4]


@pytest.mark.parametrize("point", ["after_question", "after_staged_change", "after_notice"])
async def test_s9c_session_reloaded_from_redis_mid_flow(monkeypatch, point):
    """The CallSession is JSON round-tripped (as Redis does) and a NEW realtime
    session object continues the call (media-stream reconnect)."""
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    if point == "after_question":
        await p.says("Can we do Thursday at 3 PM?")
        assert (await p.propose(**THREE))["status"] == "needs_clarification"
    elif point == "after_staged_change":
        await p.says("Move it to Thursday at 3 PM please.")
        assert (await p.propose(operation="reschedule", **THREE))["status"] == "draft"
    else:
        await p.says("Can I book another appointment, a facial Thursday at 4 PM?")
        assert (await p.propose(operation="schedule", requested_start_iso=local(T4),
                                service_description="Signature Facial"))["status"] == "draft"
    blob = json.dumps(p.session.to_dict())
    session2 = CallSession.from_dict(json.loads(blob))
    q = Phone(w, session2)
    if point == "after_notice":
        await q.says("Yes.")
        await q.confirm()
        print("RELOAD-NOTICE", q.heard, q.outputs[-1:])
        # either it books the (announced) additional appointment once or refuses; never more
        assert len(w.square.creates) <= 2 and w.square.updates == []
        return
    await q.says("Never mind, keep it.")
    out = await q.propose(**THREE)
    print("RELOAD", point, out.get("status"), q.heard)
    await q.says("Yes.")
    await q.confirm()
    await engine_yes(q)
    only_original(q)


# --------------------------------------------------------------------------- #
# 10. Ambiguous / tricky phrasings never silently create or modify
# --------------------------------------------------------------------------- #
PHRASES = [
    "keep it at 3", "keep the 2 o'clock but also add a facial", "no wait, the facial", "yes",
    "both", "whatever's easiest", "déjalo así", "otra cita", "keep me posted",
    "I'll keep that in mind", "hmm", "the 3 PM one", "sure, go ahead", "do that",
    "not sure, what do you think?", "the new one", "the original one", "no",
    "change it", "add it", "keep both", "just the one", "okay", "that one",
]


@pytest.mark.parametrize("phrase", PHRASES)
async def test_s10_ambiguous_answers(monkeypatch, phrase):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Actually, can we do Thursday at 3 PM?")
    assert (await p.propose(**THREE))["status"] == "needs_clarification"
    idx = len(p.heard)
    await p.says(phrase)
    out = await p.propose(**THREE)          # model re-sends the 3 PM proposal
    after = p.heard[idx:]
    await p.says("Yes.")                     # caller says yes to whatever was read back
    await p.confirm()
    sq = p.square()
    extra = len(w.square.creates) - 1
    moved = len(w.square.updates)
    announced_additional = any("additional appointment" in h for h in after)
    announced_move = any(("available" in h.lower() or "move" in h.lower()) for h in after)
    note("PHRASE", True, f"{phrase!r} status={out.get('status')} extra={extra} moved={moved} heard={after}")
    # Silent writes are forbidden: an extra booking needs an additional notice,
    # a move needs a move read-back.
    assert not (extra and not announced_additional), ("silent additional booking", phrase, after)
    assert not (moved and not announced_move), ("silent modification", phrase, after)
    # an additional booking for an unclear answer is wrong even if announced
    if phrase in {"yes", "whatever's easiest", "the new one", "okay", "hmm", "do that", "sure, go ahead",
                  "the 3 PM one", "that one", "just the one", "no", "no wait, the facial",
                  "keep it at 3", "keep me posted", "I'll keep that in mind", "déjalo así", "otra cita"}:
        assert extra == 0, ("unclear answer produced an additional booking", phrase, sq)
    if phrase not in {"change it"}:
        assert moved == 0, ("non-change answer moved the booking", phrase, sq)
