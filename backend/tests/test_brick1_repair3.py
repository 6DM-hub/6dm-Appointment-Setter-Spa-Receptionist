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


# --- R3-F4: a dashboard change with an unknown outcome blocks every path ---
from datetime import timedelta  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from tests.test_cara_native_double_booking import rubyz_db  # noqa: E402,F401
from tests.test_codex_brick1_tenant_boundaries import book, session_for, setup_tenants  # noqa: E402
from tests.test_brick1_booking_change_guard import T3  # noqa: E402


def _writes(square):
    return len([c for c in square.calls if c[0] == "PUT" or c[1].endswith("/cancel")])


async def test_dashboard_unknown_move_blocks_a_later_voice_cancel(rubyz_db, monkeypatch):
    from app.api.v1.appointments import update_appointment
    from app.models.booking_escalation import BookingEscalation
    from app.schemas import AppointmentUpdate
    from app.services import appointment_booking_service as booking
    from sqlalchemy import select

    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    call = session_for(a, "r3-own-dash")
    row = await book(db, a, call)
    row_id, scope = row.id, call.scope
    square = squares["fake-cleaning-token"]

    async def lose(method, route, *, json=None):
        result = await square(method, route, json=json)
        if method == "PUT":
            raise BookingProviderError("lost move response", code="BOOKING_OUTCOME_UNKNOWN")
        return result

    squares["fake-cleaning-token"] = lose
    with pytest.raises(HTTPException) as err:
        await update_appointment(row_id, AppointmentUpdate(start_time=T3, end_time=T3 + timedelta(hours=1)),
                                 scope=scope, db=db)
    assert err.value.status_code == 502
    await db.rollback()
    rows = (await db.execute(select(BookingEscalation))).scalars().all()
    assert any((r.details or {}).get("appointment_id") == str(row_id) and r.status == "pending" for r in rows)

    squares["fake-cleaning-token"] = square
    before = _writes(square)
    await db.refresh(a)
    later = session_for(a, "r3-own-later")
    later.add_turn("user", "Please cancel my appointment.")
    result = await booking.cancel_booking(db, later)
    assert result.outcome == booking.BookingOutcome.ERROR
    assert _writes(square) == before


async def test_unresolved_change_keeps_identifiers_that_look_like_card_digits(rubyz_db):
    """Payment redaction must not corrupt the appointment id the block is keyed on."""
    from app.services.booking_escalations import record_unresolved_change, unresolved_change_for
    from app.services.secure_payment import redact_payment_text

    db, spa = rubyz_db
    tenant = spa.id
    appointment_id = "c46a7751-08d4-4e88-8980-94866106c88d"   # observed to be redacted
    assert redact_payment_text(appointment_id) != appointment_id
    await record_unresolved_change(db, tenant, "call-x", {"appointment_id": appointment_id})
    assert await unresolved_change_for(db, tenant, appointment_id) is not None
