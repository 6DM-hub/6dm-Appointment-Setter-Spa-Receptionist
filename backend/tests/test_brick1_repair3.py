"""Round-3 repairs (R3-F1..F5). Provider transport simulated; no live services."""
from types import SimpleNamespace

from app.services.booking_state import answering_unrelated_question, answers_backend_question

Q = "Thursday at 2 PM is available. Would you like me to book that?"


def _session(*turns):
    return SimpleNamespace(history=[{"role": r, "content": c} for r, c in turns], entities={})


# --- R3-F1 / R3-F2: a yes binds only to the exact question -----------------
def test_exact_question_then_yes_binds():
    s = _session(("assistant", Q), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q) is True


def test_transcript_punctuation_and_case_do_not_matter_for_the_exact_question():
    s = _session(("assistant", "thursday at 2 pm is available would you like me to book that"), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q) is True


def test_unpunctuated_unrelated_question_unbinds():
    s = _session(("assistant", Q), ("assistant", "Would you like our address"), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q) is False


def test_trusted_question_plus_another_question_unbinds():
    s = _session(("assistant", Q + " Would you like our address before deciding?"), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q) is False


def test_any_untrusted_assistant_statement_unbinds():
    s = _session(("assistant", Q), ("assistant", "We also have parking out back."), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q) is False


def test_backend_spoken_line_in_between_keeps_binding():
    note = "One moment while I check that."
    s = _session(("assistant", Q), ("assistant", note), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q, trusted=[note]) is True


def test_unbound_fallback_treats_unpunctuated_question_as_a_question():
    s = _session(("assistant", "Would you like our address"), ("user", "Yes."))
    assert answering_unrelated_question(s) is True
    s = _session(("assistant", "Great. Do you need directions"), ("user", "Yes."))
    assert answering_unrelated_question(s) is True


def test_unbound_fallback_booking_question_without_mark_is_still_the_booking():
    s = _session(("assistant", "Shall I book that for you"), ("user", "Yes."))
    assert answering_unrelated_question(s) is False


# --- R3-F3: the object used as proof of cancellation must be THIS booking --
import copy  # noqa: E402

from app.services.booking_adapters.base import BookingProviderError  # noqa: E402
from tests.test_brick1_booking_change_guard import booked_call, make_world  # noqa: E402


async def _thin_cancel_then(monkeypatch, readback):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__
    state = {"cancelled": False}

    async def transport(method, route, *, json=None):
        if method == "POST" and route.endswith("/cancel"):
            w.square.calls.append((method, route, copy.deepcopy(json)))
            state["cancelled"] = True
            return {"booking": {"id": route.split("/")[-2]}}
        if method == "GET" and state["cancelled"]:
            return await readback(route)
        return await original(method, route, json=json)

    monkeypatch.setattr(w.adapter.delegate, "_request", transport)
    await p.says("Please cancel my appointment.")
    await p.tool("cancel_appointment")
    await p.says("Yes.")
    return w, p


async def test_thin_cancel_with_failed_readback_is_unknown_not_cancelled(monkeypatch):
    async def fails(route):
        raise BookingProviderError("simulated read-back timeout", code="TIMEOUT")
    w, p = await _thin_cancel_then(monkeypatch, fails)
    assert p.session.entities.get("booking_reconciliation_required")
    assert w.store.live


async def test_thin_cancel_with_readback_without_id_is_unknown(monkeypatch):
    async def no_id(route):
        return {"booking": {"status": "CANCELLED_BY_CUSTOMER"}}
    w, p = await _thin_cancel_then(monkeypatch, no_id)
    assert p.session.entities.get("booking_reconciliation_required")
    assert w.store.live


async def test_thin_cancel_with_matching_cancelled_readback_succeeds(monkeypatch):
    async def matching(route):
        bid = route.rsplit("/", 1)[-1]
        return {"booking": {"id": bid, "status": "CANCELLED_BY_CUSTOMER", "version": 3}}
    w, p = await _thin_cancel_then(monkeypatch, matching)
    assert not p.session.entities.get("booking_reconciliation_required")
    assert not w.store.live
