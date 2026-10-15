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
    assert answers_backend_question(s, history_len=0, question=Q, trusted=[{"text": note, "at": 1}]) is True


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


# --- R3-F5: write-ahead fence before every external change ------------------
async def _escalations(db):
    from app.models.booking_escalation import BookingEscalation
    from sqlalchemy import select
    return (await db.execute(select(BookingEscalation))).scalars().all()


async def test_fence_is_durable_while_the_provider_write_is_in_flight(rubyz_db, monkeypatch):
    from app.api.v1.appointments import cancel_appointment
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    call = session_for(a, "r3-fence-inflight")
    row = await book(db, a, call)
    row_id, scope = row.id, call.scope
    square = squares["fake-cleaning-token"]
    seen = {}

    async def observe(method, route, *, json=None):
        if method == "POST" and route.endswith("/cancel"):
            rows = await _escalations(db)
            seen["fences"] = [r.details.get("stage") for r in rows
                              if r.details.get("appointment_id") == str(row_id)]
        return await square(method, route, json=json)

    squares["fake-cleaning-token"] = observe
    await cancel_appointment(row_id, scope=scope, db=db)
    assert seen["fences"] == ["in_flight"]          # recorded BEFORE the write
    assert not [r for r in await _escalations(db)   # removed once verified
                if r.details.get("appointment_id") == str(row_id)]


async def test_definite_dashboard_refusal_clears_the_fence(rubyz_db, monkeypatch):
    from app.api.v1.appointments import cancel_appointment
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    call = session_for(a, "r3-fence-refused")
    row = await book(db, a, call)
    row_id, scope = row.id, call.scope
    square = squares["fake-cleaning-token"]

    async def refuse(method, route, *, json=None):
        if method == "POST" and route.endswith("/cancel"):
            raise BookingProviderError("simulated forbidden", code="FORBIDDEN")
        return await square(method, route, json=json)

    squares["fake-cleaning-token"] = refuse
    with pytest.raises(HTTPException) as err:
        await cancel_appointment(row_id, scope=scope, db=db)
    assert err.value.status_code == 409
    await db.rollback()
    assert not [r for r in await _escalations(db) if r.details.get("appointment_id") == str(row_id)]


async def test_unknown_outcome_turns_the_fence_into_a_staff_alert(rubyz_db, monkeypatch):
    from app.api.v1.appointments import cancel_appointment
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    call = session_for(a, "r3-fence-unknown")
    row = await book(db, a, call)
    row_id, scope = row.id, call.scope
    square = squares["fake-cleaning-token"]

    async def lose(method, route, *, json=None):
        if method == "POST" and route.endswith("/cancel"):
            raise BookingProviderError("lost", code="BOOKING_OUTCOME_UNKNOWN")
        return await square(method, route, json=json)

    squares["fake-cleaning-token"] = lose
    with pytest.raises(HTTPException):
        await cancel_appointment(row_id, scope=scope, db=db)
    await db.rollback()
    rows = [r for r in await _escalations(db) if r.details.get("appointment_id") == str(row_id)]
    assert len(rows) == 1                     # one record: the fence, now an alert
    assert rows[0].details["stage"] == "unknown_outcome"
    assert rows[0].delivery_status == "pending" and rows[0].status == "pending"


async def test_no_fence_no_send(rubyz_db, monkeypatch):
    from app.api.v1.appointments import cancel_appointment
    from app.services import appointment_booking_service as booking
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    call = session_for(a, "r3-no-fence")
    row = await book(db, a, call)
    row_id, scope = row.id, call.scope
    square = squares["fake-cleaning-token"]

    async def broken(*args, **kwargs):
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(booking, "_open_change_fence", broken)
    before = _writes(square)
    with pytest.raises(HTTPException) as err:
        await cancel_appointment(row_id, scope=scope, db=db)
    assert err.value.status_code == 503
    later = session_for(a, "r3-no-fence-voice")
    later.add_turn("user", "Please cancel my appointment.")
    await booking.cancel_booking(db, later)
    later.add_turn("user", "Yes.")
    result = await booking.cancel_booking(db, later)
    assert result.outcome == booking.BookingOutcome.ERROR
    assert _writes(square) == before


async def test_interrupted_fence_is_promoted_to_a_staff_alert(rubyz_db):
    from app.services.booking_escalations import open_change_fence, promote_stale_change_fences
    db, spa = rubyz_db
    await open_change_fence(db, spa.id, "call-x", {"appointment_id": "a-1", "operation": "move"})
    assert not await promote_stale_change_fences(db)                       # still fresh
    assert await promote_stale_change_fences(db, older_than=timedelta(0))  # now stale
    [row] = await _escalations(db)
    assert row.details["stage"] == "interrupted" and row.delivery_status == "pending"


async def test_fence_is_exclusive_per_appointment(rubyz_db):
    from app.services.booking_escalations import (
        ChangeInFlight, close_change_fence, open_change_fence,
    )
    db, spa = rubyz_db
    first = await open_change_fence(db, spa.id, "call-1", {"appointment_id": "a-1"})
    with pytest.raises(ChangeInFlight):
        await open_change_fence(db, spa.id, "call-2", {"appointment_id": "a-1"})
    assert await open_change_fence(db, spa.id, "call-3", {"appointment_id": "a-2"})  # other appointment
    await close_change_fence(db, first, own_transaction=True)
    assert await open_change_fence(db, spa.id, "call-2", {"appointment_id": "a-1"})


# --- Review fix: a server error on a Square booking write is never "refused" -
import httpx  # noqa: E402
from types import SimpleNamespace as _NS  # noqa: E402

from app.services.booking_adapters.providers.square import SquareAdapter  # noqa: E402

_REAL_SQUARE_REQUEST = SquareAdapter._request   # before any world patches it


async def _square_answers(monkeypatch, method, path, status, body="<html>Gateway Timeout</html>"):
    import app.services.booking_adapters.providers.square as sq

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, verb, url, **kw):
            return httpx.Response(status, text=body, request=httpx.Request(verb, url))

    monkeypatch.setattr(sq.httpx, "AsyncClient", _Client)
    adapter = _NS(_validate_config=lambda: None, base_url="https://square.test", _headers=lambda: {},
                  spa_name="test", _safe_square_error=lambda r: f"HTTP {r.status_code}",
                  _first_square_error_code=SquareAdapter._first_square_error_code)
    return await _REAL_SQUARE_REQUEST(adapter, method, path, json={})


@pytest.mark.parametrize("method,path", [
    ("POST", "/v2/bookings/B1/cancel"), ("PUT", "/v2/bookings/B1"), ("POST", "/v2/bookings"),
])
@pytest.mark.parametrize("status", [500, 502, 503, 504, 408])
async def test_square_server_error_on_a_booking_write_is_unknown(monkeypatch, method, path, status):
    with pytest.raises(BookingProviderError) as err:
        await _square_answers(monkeypatch, method, path, status)
    assert err.value.code == "BOOKING_OUTCOME_UNKNOWN"


async def test_square_unreadable_success_reply_to_a_booking_write_is_unknown(monkeypatch):
    with pytest.raises(BookingProviderError) as err:
        await _square_answers(monkeypatch, "POST", "/v2/bookings/B1/cancel", 200, body="not json")
    assert err.value.code == "BOOKING_OUTCOME_UNKNOWN"


async def test_square_explicit_refusal_stays_a_refusal(monkeypatch):
    body = '{"errors":[{"code":"FORBIDDEN","detail":"no"}]}'
    with pytest.raises(BookingProviderError) as err:
        await _square_answers(monkeypatch, "POST", "/v2/bookings/B1/cancel", 403, body=body)
    assert err.value.code == "FORBIDDEN"


async def test_square_server_error_on_a_read_is_not_a_write_outcome(monkeypatch):
    with pytest.raises(BookingProviderError) as err:
        await _square_answers(monkeypatch, "GET", "/v2/bookings/B1", 503)
    assert err.value.code != "BOOKING_OUTCOME_UNKNOWN"


# --- Review fix: fence lifecycle -------------------------------------------
async def test_a_fence_whose_ack_was_lost_is_discarded_and_does_not_block(rubyz_db, monkeypatch):
    import asyncio
    from app.api.v1.appointments import cancel_appointment
    from app.services import appointment_booking_service as booking
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    call = session_for(a, "r3-lost-ack")
    row = await book(db, a, call)
    row_id, scope = row.id, call.scope
    real_open = booking._open_change_fence

    async def slow_ack(*args, **kwargs):
        fence = await real_open(*args, **kwargs)   # committed...
        await asyncio.sleep(0.2)                    # ...but the answer comes too late
        return fence

    monkeypatch.setattr(booking, "FENCE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(booking, "_open_change_fence", slow_ack)
    with pytest.raises(HTTPException) as err:
        await cancel_appointment(row_id, scope=scope, db=db)
    assert err.value.status_code == 503
    await db.rollback()
    assert not [r for r in await _escalations(db) if r.details.get("appointment_id") == str(row_id)]
    monkeypatch.setattr(booking, "_open_change_fence", real_open)
    monkeypatch.setattr(booking, "FENCE_TIMEOUT_SECONDS", 5.0)
    await cancel_appointment(row_id, scope=scope, db=db)   # a retry is not blocked


async def test_staff_cannot_resolve_a_change_still_being_sent(rubyz_db):
    from app.services.booking_escalations import (
        mark_change_unknown, open_change_fence, resolve,
    )
    db, spa = rubyz_db
    fence = await open_change_fence(db, spa.id, "call-x", {"appointment_id": "a-1"})
    [row] = await _escalations(db)
    with pytest.raises(HTTPException) as err:
        await resolve(db, spa.id, fence, "staff-1", "unable_to_book", row.version, "checked")
    assert err.value.status_code == 409
    await mark_change_unknown(db, fence, {"code": "BOOKING_OUTCOME_UNKNOWN"})
    [row] = await _escalations(db)
    await db.refresh(row)
    resolved = await resolve(db, spa.id, fence, "staff-1", "unable_to_book", row.version, "checked in Square")
    assert resolved.status == "unable_to_book"   # an alert (outcome unknown) can be resolved


async def test_promotion_never_overwrites_an_unknown_outcome(rubyz_db):
    from app.services.booking_escalations import (
        mark_change_unknown, open_change_fence, promote_stale_change_fences,
    )
    db, spa = rubyz_db
    fence = await open_change_fence(db, spa.id, "call-x", {"appointment_id": "a-1"})
    await mark_change_unknown(db, fence, {"code": "BOOKING_OUTCOME_UNKNOWN"})
    assert not await promote_stale_change_fences(db, older_than=timedelta(0))
    [row] = await _escalations(db)
    await db.refresh(row)
    assert row.details["stage"] == "unknown_outcome" and row.details["code"] == "BOOKING_OUTCOME_UNKNOWN"


# --- Review fix: backend lines and the unbound fallback ---------------------
def test_a_backend_statement_between_question_and_yes_keeps_the_binding():
    hold = "One moment, I'm still checking that."
    s = _session(("assistant", Q), ("assistant", hold), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q, trusted=[{"text": hold, "at": 1}]) is True


def test_a_backend_question_unbinds_a_cancellation_yes_but_not_a_booking_follow_up():
    other = "Would you like me to text you our address"
    s = _session(("assistant", Q), ("assistant", other), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q, trusted=[{"text": other, "at": 1}],
                                    trust_backend_questions=False) is False
    # Booking: the backend's own consent follow-ups ("Would you like me to go
    # ahead?", a translated read-back) are questions and must keep binding.
    follow_up = "Would you like me to go ahead?"
    s = _session(("assistant", Q), ("assistant", follow_up), ("user", "Yes."))
    assert answers_backend_question(s, history_len=0, question=Q, trusted=[{"text": follow_up, "at": 1}]) is True


def test_cancellation_yes_survives_a_backend_hold_line_but_not_a_backend_question():
    from app.services import booking_change_guard as guard
    question = "Just to confirm, would you like me to cancel your House Cleaning on Thursday at 2 PM?"
    for between, expected in (("One moment, please.", "confirmed"),
                              ("Would you like our address", "none")):
        s = _session(("user", "Please cancel my appointment."))
        s.entities[guard.PENDING_CANCELLATION] = {"appointment_id": "a-1", "question": question,
                                                  "history_len": 1, "turn": 1}
        s.entities["backend_prompts"] = [{"text": between, "at": 2}]
        s.history += [{"role": "assistant", "content": question},
                      {"role": "assistant", "content": between},
                      {"role": "user", "content": "Yes."}]
        assert guard.cancellation_decision(s) == expected, between


def test_unbound_fallback_mid_sentence_and_other_topic_questions_are_unrelated():
    assert answering_unrelated_question(_session(("assistant", "Sure, would you like our address"),
                                                 ("user", "Yes."))) is True
    assert answering_unrelated_question(_session(("assistant", "Want me to text you our address?"),
                                                 ("user", "Yes."))) is True


# --- R4-F2: Google Calendar never reports a failed change as done ----------
async def _google_world(rubyz_db, monkeypatch, *, configured=True, external_id="google-event-1"):
    from types import SimpleNamespace as NS
    from app.models import BookingProvider
    from app.services import appointment_booking_service as booking
    from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    call = session_for(a, "r4-own-google")
    row = await book(db, a, call)
    row.booking_provider = BookingProvider.GOOGLE_CALENDAR.value
    row.external_booking_id = external_id
    await db.commit()
    adapter = GoogleCalendarAdapter(calendar_id="cal", connection=NS() if configured else None)
    monkeypatch.setattr(booking, "get_booking_adapter", lambda **kw: adapter)
    return db, row.id, call.scope, row


@pytest.mark.parametrize("operation", ["move", "cancel"])
async def test_google_server_error_is_unknown_and_keeps_the_block(rubyz_db, monkeypatch, operation):
    from app.api.v1.appointments import cancel_appointment, update_appointment
    from app.schemas import AppointmentUpdate
    from app.services.booking_adapters import google_calendar as gcal
    from app.services.google_calendar import GoogleCalendarError
    db, row_id, scope, row = await _google_world(rubyz_db, monkeypatch)

    async def lost(*args, **kwargs):
        raise GoogleCalendarError("google_api_error")

    monkeypatch.setattr(gcal, "update_event", lost)
    monkeypatch.setattr(gcal, "delete_event", lost)
    with pytest.raises(HTTPException) as err:
        if operation == "cancel":
            await cancel_appointment(row_id, scope=scope, db=db)
        else:
            await update_appointment(row_id, AppointmentUpdate(start_time=T3, end_time=T3 + timedelta(hours=1)),
                                     scope=scope, db=db)
    assert err.value.status_code == 502
    await db.rollback()
    [fence] = [r for r in await _escalations(db) if r.details.get("appointment_id") == str(row_id)]
    assert fence.details["stage"] == "unknown_outcome"


async def test_google_success_still_changes_the_local_row(rubyz_db, monkeypatch):
    from app.api.v1.appointments import cancel_appointment
    from app.models import AppointmentStatus
    from app.services.booking_adapters import google_calendar as gcal
    db, row_id, scope, row = await _google_world(rubyz_db, monkeypatch)
    deleted = []

    async def ok(connection, event_id):
        deleted.append(event_id)

    monkeypatch.setattr(gcal, "delete_event", ok)
    await cancel_appointment(row_id, scope=scope, db=db)
    await db.refresh(row)
    assert deleted == ["google-event-1"] and row.status == AppointmentStatus.CANCELLED
    assert not [r for r in await _escalations(db) if r.details.get("appointment_id") == str(row_id)]


# --- R4-L1: no tenant, no durable record, no external change ----------------
async def test_owner_scoped_dashboard_change_of_an_external_booking_is_refused(monkeypatch):
    import uuid as _uuid
    from types import SimpleNamespace as NS
    from app.services import appointment_booking_service as booking
    sent = []

    class Adapter:
        provider = "google_calendar"

        async def cancel_booking(self, b):
            sent.append(b)

    monkeypatch.setattr(booking, "_load_spa", lambda db, scope: _async(NS(name="sales")))
    monkeypatch.setattr(booking, "get_booking_adapter", lambda **kw: Adapter())
    appointment = NS(id=_uuid.uuid4(), external_booking_id="evt-1", booking_provider=None,
                     start_time=None, title="Demo")
    scope = NS(tenant_id=None, owner_id=_uuid.uuid4())
    with pytest.raises(booking.StaffChangeRefused) as err:
        await booking.apply_staff_change(None, scope, appointment, cancel=True)
    assert err.value.status_code == 409 and sent == []


async def _async(value):
    return value

