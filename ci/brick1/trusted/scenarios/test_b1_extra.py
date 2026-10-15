"""Extra interaction checks (SIMULATED fake Square)."""
import sys; sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from harness import *
SW="Swedish Massage"
def log(tag, d):
    open(__import__("os").path.join(__import__("os").environ.get("B1_LOG_DIR", __import__("os").path.dirname(__import__("os").path.abspath(__file__))), "extra.log"),"a").write(f"{tag}\t{d}\n")

async def test_card_on_file_additional(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    p.session.entities["card_on_file_required"] = True
    n=len(p.heard)
    await p.says("Can I also book another appointment, a Signature Facial Thursday at 4 PM?")
    await p.propose(operation="schedule", requested_start_iso=local(T4), service_description="Signature Facial", caller_name="Ada Lovelace")
    seq=[]
    for ans in ["Yes.", "Yes.", "Yes.", "Yes."]:
        await p.says(ans); seq.append((ans, p.heard[-1][:90], len(w.square.creates)))
    log("card", seq)
    assert len(w.square.creates) <= 2 and w.square.updates == []
    assert any("additional appointment" in h for h in p.heard[n:])

async def test_keep_after_two_bookings(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can I book another appointment, a Signature Facial Thursday at 4 PM?")
    await p.propose(operation="schedule", requested_start_iso=local(T4), service_description="Signature Facial", caller_name="Ada Lovelace")
    await p.says("Yes.")
    assert sorted(p.starts()) == [Z2, Z4], p.heard[-2:]
    await p.says("Hmm, can we do Thursday at 5 PM?")
    a = await p.propose(requested_start_iso=local(T5), service_description="Signature Facial", caller_name="Ada Lovelace")
    await p.says("Keep it.")
    k = await p.propose(requested_start_iso=local(T5), service_description="Signature Facial", caller_name="Ada Lovelace")
    log("two", f"{a.get('status')} {k.get('status')} {k.get('spoken')}")
    await p.says("Yes."); await p.confirm(); await engine_yes(p)
    assert sorted(p.starts()) == [Z2, Z4] and len(w.square.creates) == 2 and w.square.updates == []
    assert "Signature Facial on Thursday at 4 PM" in (k.get("spoken") or "")

async def test_move_which_one_then_never_mind(monkeypatch):
    w = make_world(monkeypatch)
    p = await book_2pm(w)
    await p.says("Can I book another appointment, a Signature Facial Thursday at 4 PM?")
    await p.propose(operation="schedule", requested_start_iso=local(T4), service_description="Signature Facial", caller_name="Ada Lovelace")
    await p.says("Yes.")
    await p.says("Can you move my massage to Thursday at 5 PM instead?")
    a = await p.propose(operation="reschedule", requested_start_iso=local(T5), service_description=SW)
    await p.says("Never mind, leave everything as it is.")
    k = await p.start_new()
    await p.says("Yes."); await p.confirm(); await engine_yes(p)
    log("which", f"{a.get('status')} {k.get('status')} {p.square()}")
    assert sorted(p.starts()) == [Z2, Z4] and len(w.square.creates) == 2 and w.square.updates == []
