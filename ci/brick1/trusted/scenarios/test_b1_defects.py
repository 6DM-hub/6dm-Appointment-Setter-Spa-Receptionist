"""Targeted reproductions of suspected Brick 1 defects. SIMULATED (fake Square)."""
import sys

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import json

from harness import T2, T3, T4, T5, Z2, Z3, Z4, Z5, Phone, book_2pm, engine_yes, local, make_world
from app.services.booking_conversation import offer_accepted
from app.services.booking_state import get_draft
from app.services.call_state import CallSession

SW = "Swedish Massage"
THREE = dict(requested_start_iso=local(T3), service_description=SW, caller_name="Ada Lovelace")


def log(tag, detail):
    with open(__import__("os").path.join(__import__("os").environ.get("B1_LOG_DIR", __import__("os").path.dirname(__import__("os").path.abspath(__file__))), "defects.log"), "a") as fh:
        fh.write(f"{tag}\t{detail}\n")


# D-A: the clarification question and the "stays exactly as it is" line are
# never spoken when the original booking's offer is still marked accepted.
async def test_dA_question_and_keep_line_are_audible(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    log("dA offer_accepted_after_booking", offer_accepted(p.session))
    n = len(p.heard)
    await p.says("Actually, can we do Thursday at 3 PM?")
    asked = await p.propose(**THREE)
    q = p.heard[n:]
    n = len(p.heard)
    await p.says("Keep it.")
    kept = await p.propose(**THREE)
    k = p.heard[n:]
    log("dA", f"asked={asked['status']} heard={q} | kept={kept['status']} heard={k} spoken_field={kept.get('spoken')!r}")
    assert any("keep everything as it is" in h for h in q), ("question never spoken", q)
    assert any("stays exactly as it is" in h for h in k), ("keep confirmation never spoken", k)


async def test_dA_root_cause_offer_flag(monkeypatch):
    """Same flow with the stale accepted-offer flag removed: speech is delivered."""
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    p.session.entities.pop("accepted_booking_offer", None)
    n = len(p.heard)
    await p.says("Actually, can we do Thursday at 3 PM?")
    await p.propose(**THREE)
    await p.says("Keep it.")
    await p.propose(**THREE)
    log("dA-root", p.heard[n:])
    assert any("stays exactly as it is" in h for h in p.heard[n:])


# D-B: "never mind" after a staged change, model replies without a tool call,
# caller's next bare "yes" commits the abandoned change.
async def test_dB_never_mind_then_yes_to_anything_else(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can we move it to Thursday at 3 PM instead?")
    await p.propose(operation="reschedule", **THREE)
    await p.says("Never mind, keep it.")
    p.session.add_turn("assistant", "No problem, your 2 PM stays. Is there anything else I can help with?")
    await p.says("Yes.")
    log("dB", f"square={p.square()} heard_last={p.heard[-1:]}")
    assert p.starts() == [Z2] and w.square.updates == [], p.square()


# D-C: media-stream reconnect: CallSession reloaded from Redis JSON into a new
# realtime session object. Realtime-only counters (_booking_completed_turn,
# _user_turn_count) are not persisted.
async def test_dC_reload_then_keep_during_staged_change(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Move it to Thursday at 3 PM please.")
    await p.propose(operation="reschedule", **THREE)
    s2 = CallSession.from_dict(json.loads(json.dumps(p.session.to_dict())))
    q = Phone(w, s2)
    await q.says("Never mind, keep it.")
    out = await q.propose(operation="reschedule", **THREE)
    log("dC-keep", f"status={out.get('status')} completed_turn={q.voice._booking_completed_turn}")
    await q.says("Yes.")
    log("dC-keep", f"square={q.square()}")
    assert q.starts() == [Z2] and w.square.updates == []


async def test_dC_reload_after_additional_notice(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    for _ in range(4):
        await p.says("Okay.")  # some small talk so the turn counter is > 4
    await p.says("Can I book another appointment, a Signature Facial Thursday at 4 PM?")
    await p.propose(operation="schedule", requested_start_iso=local(T4),
                    service_description="Signature Facial", caller_name="Ada Lovelace")
    marker = p.session.entities.get("additional_booking_pending")
    s2 = CallSession.from_dict(json.loads(json.dumps(p.session.to_dict())))
    q = Phone(w, s2)
    n = len(q.heard)
    await q.says("Yes, please.")
    log("dC-notice", f"marker={marker} turn_after_reload={q.voice._user_turn_count} heard={q.heard[n:]} square={q.starts()}")
    assert sorted(q.starts()) == [Z2, Z4], ("caller's yes after reconnect did not book; heard:", q.heard[n:])


# D-D: backend "yes" path does not understand additional_notice_required /
# awaiting_confirmation and speaks a false failure line.
async def test_dD_conflict_alternative_for_additional(monkeypatch):
    w = make_world(monkeypatch)
    w.square.openings = [o for o in w.square.openings
                         if not (o["variation"] == "var_facial_60" and o["start"] == T4)]
    p = await book_2pm(w)
    n = len(p.heard)
    await p.says("I'd like to book another appointment, a Signature Facial on Thursday at 4 PM.")
    await p.propose(operation="schedule", requested_start_iso=local(T4),
                    service_description="Signature Facial", caller_name="Ada Lovelace")
    await p.says("5 PM works.")
    after_choice = p.heard[n:]
    log("dD", after_choice)
    assert not any("wasn't able to complete" in h for h in after_choice), after_choice


# D-E: snapshot restore leaves session-level mirrors from the abandoned change.
async def test_dE_service_switch_then_keep_then_confirm(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can I switch it to a Signature Facial instead, same time Thursday at 2 PM?")
    st = await p.propose(operation="reschedule", requested_start_iso=local(T2),
                         service_description="Signature Facial")
    await p.says("Never mind, keep the massage.")
    kept = await p.propose(operation="reschedule", requested_start_iso=local(T2),
                           service_description="Signature Facial")
    mirrors = (p.session.selected_service, p.session.selected_duration, p.session.requested_datetime,
               p.session.confirmed_datetime, p.session.booking_status)
    n = len(p.heard)
    await p.says("Yes.")
    c = await p.confirm()
    log("dE", f"staged={st.get('status')} kept={kept.get('status')} mirrors={mirrors} confirm={c.get('status')} heard={p.heard[n:]} square={p.square()}")
    assert [b["variation"] for b in p.square()] == ["var_swedish_60"] and w.square.updates == []
    assert not any("Signature Facial appointment is confirmed" in h for h in p.heard[n:]), p.heard[n:]


# D-F: legacy <Gather> text loop has no keep / change-or-add logic at all.
async def test_dF_legacy_loop_keep_it(monkeypatch):
    """Engine-level model of telephony.voice_respond: Grok extraction on
    'keep it' (confused) yields schedule @3 PM; the reply reads it back; 'yes'
    commits via record_pure_confirmation + confirm_booking."""
    from app.services.appointment_booking_service import confirm_booking, stage_booking
    from app.services.booking_state import mark_read_back, record_pure_confirmation
    from app.services.grok_service import AppointmentIntent
    w = make_world(monkeypatch)
    p = await book_2pm(w, realistic=False)
    s = p.session
    s.add_turn("user", "Keep it.")
    r = await stage_booking(w.db, s, AppointmentIntent(intent="schedule", confidence=1.0,
                            requested_start_iso=local(T3), service_description=SW, caller_name="Ada Lovelace"))
    mark_read_back(s)
    s.add_turn("user", "Yes.")
    res = None
    if record_pure_confirmation(s, "Yes."):
        res = await confirm_booking(w.db, s)
    log("dF", f"stage={r.outcome} confirm={getattr(res, 'outcome', None)} square={p.square()}")
    assert p.starts() == [Z2] and w.square.updates == [] and len(w.square.creates) == 1, p.square()


async def test_dB2_never_mind_then_yes_with_question(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can we move it to Thursday at 3 PM instead?")
    await p.propose(operation="reschedule", **THREE)
    await p.says("Never mind, keep it.")
    p.session.add_turn("assistant", "Sure, your 2 PM stays. Anything else?")
    await p.says("Yes, where are you located?")
    log("dB2", f"square={p.starts()} heard_last={p.heard[-1:]}")
    assert p.starts() == [Z2] and w.square.updates == [], p.square()
