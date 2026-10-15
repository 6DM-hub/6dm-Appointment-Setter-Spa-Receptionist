"""Brick 1, second repair round: regressions for Codex's findings F1-F8.

Codex's own cases live unchanged in test_codex_brick1_adversarial.py and
test_codex_brick1_tenant_boundaries.py. These add the behaviour that must be
preserved next to each repair, plus edge cases around it.

SIMULATED: real realtime dispatcher, real legacy voice_respond, real booking
engine and real Square adapter over the in-memory FakeSquare transport. No
real Square, no production data.
"""
from __future__ import annotations

import copy

import pytest

from app.services import appointment_booking_service as engine
from tests.test_brick1_booking_change_guard import (
    LegacyCall, Phone, T2, T3, Z2, _intent, _legacy_booked, booked_call, local, make_world,
)


def cancellation_transport(monkeypatch, world):
    """Square's booking-cancel endpoint, which square_world does not model."""
    original = world.square.__call__

    async def request(method, path, *, json=None):
        if method == "POST" and path.endswith("/cancel"):
            body = copy.deepcopy(json or {})
            world.square.calls.append((method, path, body))
            booking = world.square.bookings[path.split("/")[-2]]
            booking["status"] = "CANCELLED_BY_CUSTOMER"
            booking["version"] += 1
            return {"booking": copy.deepcopy(booking)}
        return await original(method, path, json=json)

    monkeypatch.setattr(world.adapter.delegate, "_request", request)


def cancels(world):
    return [c for c in world.square.calls if c[1].endswith("/cancel")]


# --------------------------------------------------------------------------- #
# F1: cancellation needs the caller's own, current words
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("request_text", [
    "Please cancel my appointment.", "I need to cancel it.", "Can you call it off?",
    "Could you please cancel my appointment?",
])
async def test_cancel_request_is_asked_back_then_a_yes_cancels(monkeypatch, request_text):
    """Owner-approved Brick 1 design: CARA always asks before cancelling."""
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says(request_text)
    out = await p.tool("cancel_appointment")
    assert out["status"] == "cancellation_confirmation_required", out
    assert p.heard[-1].startswith("Just to confirm, would you like me to cancel your")
    assert "Thursday at 2 PM" in p.heard[-1]
    assert not cancels(w) and p.starts() == [Z2]
    heard = await p.says("Yes.")                    # the backend owns the write
    assert len(cancels(w)) == 1 and not w.square.active()
    assert any("cancelled" in line for line in heard), heard


async def test_the_model_confirming_on_the_yes_turn_cancels_once(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("I'd like to cancel my appointment.")
    await p.tool("cancel_appointment")
    await p.says("Yes, please.")
    await p.tool("cancel_appointment")              # a duplicate/late model call
    assert len(cancels(w)) == 1 and not w.square.active()


@pytest.mark.parametrize("answer", [
    "Yes, where are you located?", "Hmm, what's the fee?", "Yes and also is there parking?",
])
async def test_anything_but_a_plain_yes_to_the_cancel_question_cancels_nothing(monkeypatch, answer):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("Please cancel my appointment.")
    await p.tool("cancel_appointment")
    await p.says(answer)
    assert get_pending(p) is None, "a non-yes answer withdraws the question"
    out = await p.tool("cancel_appointment")          # the model tries anyway
    assert out["status"] == "cancellation_confirmation_required", out
    assert not cancels(w) and p.starts() == [Z2]
    # Only a plain yes to the question CARA just asked (again) cancels.
    await p.says("Yes.")
    assert len(cancels(w)) == 1


def get_pending(p):
    from app.services.booking_change_guard import PENDING_CANCELLATION
    return p.session.entities.get(PENDING_CANCELLATION)


@pytest.mark.parametrize("answer", [
    "Actually, never mind.", "No, don't cancel it.", "No.", "Keep it.",
    "Cancel it... actually never mind.",
])
async def test_withdrawn_cancellation_cancels_nothing_and_says_so(monkeypatch, answer):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("I want to cancel my appointment.")
    asked = await p.tool("cancel_appointment")
    assert asked["status"] == "cancellation_confirmation_required"
    await p.says(answer)
    out = await p.tool("cancel_appointment")
    assert out["status"] == "kept_existing" and out["cancelled"] is False, out
    assert any("nothing was cancelled" in line or "haven't cancelled" in line for line in p.heard[-1:]), p.heard
    assert not cancels(w) and p.starts() == [Z2]


async def test_model_cancel_without_any_cancel_request_is_refused(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("What time does the place open on Thursday?")
    out = await p.tool("cancel_appointment")
    assert out["status"] == "cancellation_not_requested", out
    assert not cancels(w) and p.starts() == [Z2]


async def test_yes_long_after_a_withdrawn_cancel_request_cancels_nothing(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("Cancel my appointment.")
    await p.says("Wait, no, keep it.")
    await p.says("Yes, that's all.")
    await p.tool("cancel_appointment")
    assert not cancels(w) and p.starts() == [Z2]


async def test_legacy_refused_cancellation_tells_the_caller_nothing_was_cancelled(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    c = await _legacy_booked(w, monkeypatch)
    said = await c.says("Never mind, leave it.", intent=_intent("cancel", T2, w.first),
                        reply="Your appointment has been cancelled.")
    assert "cancelled" not in said.replace("nothing was cancelled", "")
    assert not cancels(w) and c.starts() == [Z2]


async def test_legacy_explicit_cancel_still_cancels(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    c = await _legacy_booked(w, monkeypatch)
    said = await c.says("Please cancel my appointment.", intent=_intent("cancel", T2, w.first),
                        reply="Your appointment is cancelled. Anything else?")
    assert said.startswith("Just to confirm, would you like me to cancel your"), said
    assert not cancels(w)
    # Whatever the extraction makes of "Yes.", the answer to CARA's question decides.
    await c.says("Yes.", intent=None, reply="Your appointment is cancelled. Anything else?")
    assert len(cancels(w)) == 1 and not w.square.active()


async def test_cancel_authorization_does_not_carry_into_a_new_call(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("Cancel my appointment please.")
    # A new call by the same customer: only that call's words count.
    c = LegacyCall(w, monkeypatch)

    async def contact(*_a, **_k):
        return w.contact

    async def upcoming(*_a, **_k):
        return w.store.live

    monkeypatch.setattr(engine, "_find_contact_by_phone", contact)
    monkeypatch.setattr(engine, "_find_upcoming_appointments", upcoming)
    await c.says("Yes.", intent=_intent("cancel", T2, w.first))
    assert not cancels(w) and c.starts() == [Z2]


# --------------------------------------------------------------------------- #
# F2 / F3 / F7: consent must answer the booking question, and only that
# --------------------------------------------------------------------------- #
async def _offered(world):
    p = Phone(world)
    await p.says(f"A {world.first} Thursday at 2 PM. My name is Ada Lovelace.")
    out = await p.propose(operation="schedule", requested_start_iso=local(T2),
                          service_description=world.first, caller_name="Ada Lovelace")
    assert out["status"] == "draft", out
    return p


@pytest.mark.parametrize("consent", [
    "Yes.", "Yes please.", "Yes, please book it.", "Yeah, that works for me, thanks.",
    "Yes, that sounds great, thank you so much.", "Okay, go ahead.",
    "Yes please, that'd be great.", "Yes, I'd like that.",
])
async def test_real_consent_still_books(monkeypatch, consent):
    w = make_world(monkeypatch)
    p = await _offered(w)
    await p.says(consent)
    assert p.starts() == [Z2] and len(w.square.creates) == 1, (consent, p.heard)


@pytest.mark.parametrize("utterance", [
    "Yes, what's the cancellation policy?", "Yes, is there parking?", "Yes, hold on a second.",
    "Yeah, I'm driving right now.", "Sure, what do I need to bring?",
    "Yes, I'd like to know the price first.",
])
async def test_yes_followed_by_something_else_is_not_consent(monkeypatch, utterance):
    w = make_world(monkeypatch)
    p = await _offered(w)
    await p.says(utterance)
    await p.tool("confirm_appointment")
    assert not w.square.creates, (utterance, p.heard)


async def test_bare_yes_to_an_unrelated_question_is_not_consent(monkeypatch):
    w = make_world(monkeypatch)
    p = await _offered(w)
    p.session.add_turn("assistant", "Would you like our address?")
    heard = await p.says("Yes.")
    await p.tool("confirm_appointment")
    assert not w.square.creates
    # CARA's backend asks the booking question again; a yes to THAT books it.
    assert heard and "Would you like me to book" in heard[-1], heard
    await p.says("Yes, please.")
    assert p.starts() == [Z2] and len(w.square.creates) == 1
    # A model-worded "booking" question is not a question CARA's backend asked.


async def test_a_model_question_between_read_back_and_yes_needs_the_backend_question(monkeypatch):
    w = make_world(monkeypatch)
    p = await _offered(w)
    p.session.add_turn("assistant", "Before I book that, would you like to hear our policies?")
    heard = await p.says("Yes.")
    assert not w.square.creates
    assert heard and "Would you like me to book" in heard[-1], heard


async def test_refused_offer_can_be_offered_again_and_booked(monkeypatch):
    w = make_world(monkeypatch)
    p = await _offered(w)
    await p.says("No, don't book that.")
    await p.says("Yes.")
    await p.tool("confirm_appointment")
    assert not w.square.creates
    # The caller changes their mind: a fresh offer is checked, read back and confirmed.
    await p.says(f"Actually, could I get the {w.first} Thursday at 2 PM after all?")
    out = await p.propose(operation="schedule", requested_start_iso=local(T2),
                          service_description=w.first, caller_name="Ada Lovelace")
    assert out["status"] == "draft" and out["spoken"], out
    await p.says("Yes, please.")
    assert p.starts() == [Z2] and len(w.square.creates) == 1


async def test_legacy_refused_offer_then_yes_to_another_question_books_nothing(monkeypatch):
    w = make_world(monkeypatch)
    c = LegacyCall(w, monkeypatch)
    await c.says(f"A {w.first} Thursday at 2 PM, I'm Ada Lovelace.",
                 intent=_intent("schedule", T2, w.first),
                 reply="Thursday at 2 PM is available. Would you like me to book that?")
    await c.says("No, don't book that.", reply="Okay. Would you like our address?")
    await c.says("Yes.", reply="We're at 1 Main Street.")
    assert not w.square.creates


@pytest.mark.parametrize("business", ["spa", "hvac", "dental"])
async def test_service_correction_books_the_corrected_service_after_fresh_consent(monkeypatch, business):
    w = make_world(monkeypatch, business)
    p = await _offered(w)
    await p.says(f"Yes, {w.second}.")
    assert not w.square.creates
    out = await p.propose(operation="schedule", requested_start_iso=local(T2),
                          service_description=w.second, caller_name="Ada Lovelace")
    assert out["status"] == "draft", out
    await p.says("Yes, please.")
    assert p.variations() == [w.second_var] and len(w.square.creates) == 1


# --------------------------------------------------------------------------- #
# F4: a change whose outcome is unknown is never described from memory
# --------------------------------------------------------------------------- #
import asyncio  # noqa: E402

import httpx  # noqa: E402

from app.services.booking_adapters.base import BookingProviderError  # noqa: E402


def lose_response(monkeypatch, world, method, *, apply=True, exc=None):
    original = world.square.__call__

    async def request(verb, path, *, json=None):
        if verb == method and (method != "POST" or path.endswith("/cancel")):
            if apply:
                await original(verb, path, json=json)
            raise exc or asyncio.TimeoutError("response lost")
        return await original(verb, path, json=json)

    monkeypatch.setattr(world.adapter.delegate, "_request", request)


async def test_lost_move_response_is_reported_honestly_and_blocks_further_writes(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    lose_response(monkeypatch, w, "PUT")
    await p.says("Move my appointment to Thursday at 3 PM instead.")
    await p.propose(operation="reschedule", requested_start_iso=local(T3), service_description=w.first)
    heard = await p.says("Yes, go ahead.")
    assert p.starts() == ["2026-10-22T20:00:00Z"]
    marker = p.session.entities["booking_reconciliation_required"]
    assert marker["operation"] == "move" and marker["provider_observed"]["start"] == "2026-10-22T20:00:00Z"
    said = " ".join(heard)
    assert "can't confirm" in said and "all set" not in said and "has been moved" not in said
    assert "Right now it shows the appointment on Thursday at 3 PM" in said
    # No further write is accepted until staff reconcile it.
    writes_before = len(w.square.updates)
    await p.says("Okay, then move it to Thursday at 4 PM instead.")
    await p.propose(operation="reschedule", requested_start_iso=local(T3 + (T3 - T2)),
                    service_description=w.first)
    await p.says("Yes, go ahead.")
    await p.tool("confirm_appointment")
    assert len(w.square.updates) == writes_before


async def test_lost_cancel_response_is_never_reported_as_cancelled_or_kept(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__

    async def request(verb, path, *, json=None):
        if verb == "POST" and path.endswith("/cancel"):
            booking = w.square.bookings[path.split("/")[-2]]
            booking["status"] = "CANCELLED_BY_CUSTOMER"
            raise asyncio.TimeoutError("response lost")
        return await original(verb, path, json=json)

    monkeypatch.setattr(w.adapter.delegate, "_request", request)
    await p.says("Please cancel my appointment.")
    await p.tool("cancel_appointment")
    answer = await p.says("Yes.")
    assert p.session.entities["booking_reconciliation_required"]["operation"] == "cancel"
    assert not any("has been cancelled" in line for line in answer), answer
    heard = await p.says("Never mind, keep it.")
    said = " ".join(p.heard[-2:] + heard)
    assert "stays exactly as it is" not in said and "has been cancelled" not in said


async def test_square_read_timeout_on_a_write_is_an_unknown_outcome(monkeypatch):
    from app.services.booking_adapters.providers.square import SquareAdapter

    adapter = SquareAdapter.__new__(SquareAdapter)
    adapter.config = {"access_token": "t", "location_id": "loc"}
    adapter.spa_name = "Test"
    monkeypatch.setattr(adapter, "_validate_config", lambda: None)
    monkeypatch.setattr(SquareAdapter, "base_url", "https://example.invalid", raising=False)
    monkeypatch.setattr(adapter, "_headers", lambda: {})

    class Client:
        def __init__(self, error):
            self.error = error

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def request(self, *_a, **_k):
            raise self.error

    for error, code, retryable in [
        (httpx.ReadTimeout("read timed out"), "BOOKING_OUTCOME_UNKNOWN", False),
        (httpx.ConnectError("refused"), None, True),
    ]:
        monkeypatch.setattr(httpx, "AsyncClient", lambda *a, _e=error, **k: Client(_e))
        with pytest.raises(BookingProviderError) as raised:
            await adapter._request("PUT", "/v2/bookings/sq_1", json={})
        assert raised.value.code == code and raised.value.retryable is retryable


# --------------------------------------------------------------------------- #
# F5 / F6: Square must hold exactly what the caller authorized
# --------------------------------------------------------------------------- #
def team_of(world):
    return [b["appointment_segments"][0]["team_member_id"] for b in world.square.active()]


async def test_time_only_move_keeps_and_verifies_the_same_staff(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    held = team_of(w)
    await p.says("Can we move it to Thursday at 3 PM instead?")
    await p.propose(operation="reschedule", requested_start_iso=local(T3), service_description=w.first)
    await p.says("Yes, go ahead.")
    assert p.starts() == ["2026-10-22T20:00:00Z"] and team_of(w) == held


async def test_staff_change_to_someone_unavailable_is_refused_not_faked(monkeypatch):
    w = make_world(monkeypatch)
    w.square.openings = [s for s in w.square.openings if not (s["start"] == T3 and s["team"] == "TM_BEA")]
    p = await booked_call(w)
    held = team_of(w)
    await p.says("Change my appointment to be with Bea on Thursday at 3 PM instead.")
    out = await p.propose(operation="reschedule", requested_start_iso=local(T3),
                          service_description=w.first, preferred_staff="Bea")
    await p.says("Yes, go ahead.")
    assert w.square.updates == [] and team_of(w) == held and p.starts() == [Z2], out


async def test_thin_create_response_is_read_back_before_success(monkeypatch):
    w = make_world(monkeypatch)
    p = await _offered(w)
    original = w.square.__call__

    async def thin(method, path, *, json=None):
        result = await original(method, path, json=json)
        if method == "POST" and path == "/v2/bookings":
            return {"booking": {"id": result["booking"]["id"], "status": "ACCEPTED"}}
        return result

    monkeypatch.setattr(w.adapter.delegate, "_request", thin)
    await p.says("Yes, please.")
    reads = [c for c in w.square.calls if c[0] == "GET" and c[1].startswith("/v2/bookings/")]
    assert reads, "a thin create response must be verified by reading the booking back"
    assert p.session.booking_status == "booked" and p.starts() == [Z2]


async def test_move_that_lands_with_the_wrong_staff_is_not_reported_as_done(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__

    async def swap_staff(method, path, *, json=None):
        result = await original(method, path, json=json)
        if method == "PUT":
            booking = w.square.bookings[path.rsplit("/", 1)[-1]]
            booking["appointment_segments"][0]["team_member_id"] = "TM_SOMEONE_ELSE"
            return {"booking": copy.deepcopy(booking)}
        return result

    monkeypatch.setattr(w.adapter.delegate, "_request", swap_staff)
    await p.says("Can we move it to Thursday at 3 PM instead?")
    await p.propose(operation="reschedule", requested_start_iso=local(T3), service_description=w.first)
    heard = await p.says("Yes, go ahead.")
    assert p.session.entities.get("booking_reconciliation_required")
    assert not any("all set" in line or "has been moved" in line for line in heard), heard


# --------------------------------------------------------------------------- #
# F8: dashboard edits keep the calendar of record and CARA in agreement
# --------------------------------------------------------------------------- #
from datetime import timedelta  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from app.models import AppointmentStatus  # noqa: E402
from app.schemas import AppointmentUpdate  # noqa: E402
from tests.square_world import THU_2PM_UTC  # noqa: E402
from tests.test_cara_native_double_booking import rubyz_db  # noqa: E402,F401
from tests.test_codex_brick1_tenant_boundaries import book, session_for, setup_tenants  # noqa: E402


async def test_dashboard_move_refused_by_the_calendar_changes_nothing_locally(rubyz_db, monkeypatch):
    from app.api.v1.appointments import update_appointment

    db, a, _b, squares = await setup_tenants(rubyz_db, monkeypatch)
    session = session_for(a, "refused")
    row = await book(db, a, session)
    square = squares["fake-cleaning-token"]
    original = square.__call__

    async def refuse(method, path, *, json=None):
        if method == "PUT":
            raise BookingProviderError("Square API request failed: BAD_REQUEST", code="BAD_REQUEST")
        return await original(method, path, json=json)

    squares["fake-cleaning-token"] = refuse
    with pytest.raises(HTTPException) as raised:
        await update_appointment(row.id, AppointmentUpdate(
            start_time=THU_2PM_UTC + timedelta(hours=2), end_time=THU_2PM_UTC + timedelta(hours=3),
        ), scope=session.scope, db=db)
    assert raised.value.status_code == 409
    await db.refresh(row)
    assert row.start_time.replace(tzinfo=None) == THU_2PM_UTC.replace(tzinfo=None)
    assert square.active()[0]["start_at"] == Z2


async def test_dashboard_move_with_unknown_outcome_leaves_the_row_and_says_so(rubyz_db, monkeypatch):
    from app.api.v1.appointments import update_appointment

    db, a, _b, squares = await setup_tenants(rubyz_db, monkeypatch)
    session = session_for(a, "unknown")
    row = await book(db, a, session)
    square = squares["fake-cleaning-token"]

    async def lost(method, path, *, json=None):
        result = await square(method, path, json=json)
        if method == "PUT":
            raise asyncio.TimeoutError("response lost")
        return result

    squares["fake-cleaning-token"] = lost
    with pytest.raises(HTTPException) as raised:
        await update_appointment(row.id, AppointmentUpdate(
            start_time=THU_2PM_UTC + timedelta(hours=2), end_time=THU_2PM_UTC + timedelta(hours=3),
        ), scope=session.scope, db=db)
    assert raised.value.status_code == 502 and "unknown" in raised.value.detail
    await db.refresh(row)
    assert row.start_time.replace(tzinfo=None) == THU_2PM_UTC.replace(tzinfo=None)


async def test_dashboard_status_cancel_reaches_the_calendar(rubyz_db, monkeypatch):
    from app.api.v1.appointments import update_appointment

    db, a, _b, squares = await setup_tenants(rubyz_db, monkeypatch)
    session = session_for(a, "status")
    row = await book(db, a, session)
    await update_appointment(row.id, AppointmentUpdate(status=AppointmentStatus.CANCELLED),
                             scope=session.scope, db=db)
    assert not squares["fake-cleaning-token"].active()
    assert row.status == AppointmentStatus.CANCELLED


async def test_dashboard_title_edit_does_not_touch_the_calendar(rubyz_db, monkeypatch):
    from app.api.v1.appointments import update_appointment

    db, a, _b, squares = await setup_tenants(rubyz_db, monkeypatch)
    session = session_for(a, "title")
    row = await book(db, a, session)
    calls = len(squares["fake-cleaning-token"].calls)
    await update_appointment(row.id, AppointmentUpdate(title="House Cleaning (side door)"),
                             scope=session.scope, db=db)
    assert len(squares["fake-cleaning-token"].calls) == calls
    assert row.title == "House Cleaning (side door)"


async def test_dashboard_cannot_change_another_tenants_appointment(rubyz_db, monkeypatch):
    from app.api.v1.appointments import cancel_appointment

    db, a, b, squares = await setup_tenants(rubyz_db, monkeypatch)
    row = await book(db, a, session_for(a, "owner"))
    with pytest.raises(HTTPException) as raised:
        await cancel_appointment(row.id, scope=session_for(b, "other").scope, db=db)
    assert raised.value.status_code == 404
    assert len(squares["fake-cleaning-token"].active()) == 1


# --------------------------------------------------------------------------- #
# Verification gap: an offer that went stale before the caller said yes
# --------------------------------------------------------------------------- #
async def test_offer_taken_by_someone_else_before_yes_is_never_booked(monkeypatch):
    w = make_world(monkeypatch)
    p = await _offered(w)
    # Both team members are booked at 2 PM by other customers in the meantime.
    for n, team in enumerate(("TM_ANA", "TM_BEA")):
        w.square.bookings[f"other_{n}"] = {
            "id": f"other_{n}", "status": "ACCEPTED", "version": 1, "start_at": Z2,
            "location_id": "loc_123", "customer_id": f"cust_other_{n}",
            "appointment_segments": [{"team_member_id": team, "service_variation_id": w.first_var,
                                      "service_variation_version": 3, "duration_minutes": 60}],
        }
    heard = await p.says("Yes, please.")
    assert not w.square.creates, "a slot that is no longer free must not be booked"
    assert p.session.booking_status != "booked"
    assert not any("all set" in line for line in heard), heard


# --------------------------------------------------------------------------- #
# R2-F1 / R2-F3: the cancel question is bound to its own answer
# --------------------------------------------------------------------------- #
from tests.test_brick1_booking_change_guard import reconnect  # noqa: E402


async def test_yes_after_another_question_does_not_answer_the_cancel_question(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("Please cancel my appointment.")
    await p.tool("cancel_appointment")
    p.session.add_turn("assistant", p.heard[-1])                 # CARA's question, as transcribed
    p.session.add_turn("assistant", "Would you like our address before you go?")
    await p.says("Yes.")
    await p.tool("cancel_appointment")
    assert not cancels(w) and p.starts() == [Z2]


async def test_the_cancel_question_survives_a_reconnect(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("Please cancel my appointment.")
    await p.tool("cancel_appointment")
    q = reconnect(p)
    await q.says("Yes.")
    assert len(cancels(w)) == 1 and not w.square.active()


@pytest.mark.parametrize("words", [
    "Can you tell me if I can cancel?", "I might cancel later.", "What happens if I cancel?",
    "Never cancel my appointment.", "Is there a cancellation fee?",
])
async def test_talking_about_cancelling_never_even_asks(monkeypatch, words):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says(words)
    out = await p.tool("cancel_appointment")
    assert out["status"] in {"cancellation_not_requested", "kept_existing"}, (words, out)
    assert not cancels(w) and p.starts() == [Z2]


# --------------------------------------------------------------------------- #
# R2-F2: a yes binds to the booking question it follows
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question", [
    "Do you need directions?", "Shall I text you our address?", "Can I help with anything else first?",
])
async def test_legacy_yes_to_another_question_never_books(monkeypatch, question):
    w = make_world(monkeypatch)
    c = LegacyCall(w, monkeypatch)
    await c.says(f"A {w.first} Thursday at 2 PM, I'm Ada Lovelace.", intent=_intent("schedule", T2, w.first),
                 reply="Thursday at 2 PM is available. Would you like me to book that?")
    await c.says("Hmm, one second.", reply=question)
    await c.says("Yes.", reply="Sure.")
    assert not w.square.creates
    # The reply re-reads the booking question: now a yes books it.
    await c.says("Book the 2 PM please.", reply="Thursday at 2 PM is open. Would you like me to book that?")
    await c.says("Yes.", reply="Done.")
    assert len(w.square.creates) == 1


async def test_backend_spoken_lines_in_between_do_not_break_consent(monkeypatch):
    w = make_world(monkeypatch)
    p = await _offered(w)
    # CARA's own (backend) lines are transcribed into the history in production.
    for line in p.heard:
        p.session.add_turn("assistant", line)
    await p.says("Yes, please.")
    assert p.starts() == [Z2] and len(w.square.creates) == 1


def test_policy_question_mentioning_book_does_not_arm_a_read_back(monkeypatch):
    from app.services.xai_realtime import _CONFIRMATION_QUESTION_RE
    text = "Would you like me to explain our cancellation policy before you book?"
    # The pattern alone would match; the read-back also has to name the time.
    assert not _CONFIRMATION_QUESTION_RE.search(text) or "PM" not in text


# --------------------------------------------------------------------------- #
# R2-F4 / R2-F5: a cancellation is done only when Square says so, exactly
# --------------------------------------------------------------------------- #
def cancel_reply(monkeypatch, world, *, raise_code=None, reply=None):
    original = world.square.__call__

    async def request(method, path, *, json=None):
        if method == "POST" and path.endswith("/cancel"):
            world.square.calls.append((method, path, json or {}))
            if raise_code:
                raise BookingProviderError("simulated Square answer", code=raise_code)
            return {"booking": copy.deepcopy(reply)}
        return await original(method, path, json=json)

    monkeypatch.setattr(world.adapter.delegate, "_request", request)


async def _confirmed_cancel(p):
    await p.says("Please cancel my appointment.")
    await p.tool("cancel_appointment")
    return await p.says("Yes.")


@pytest.mark.parametrize("code", ["FORBIDDEN", "CONFLICT", "BOOKING_OUTCOME_UNKNOWN"])
async def test_voice_cancel_square_refusal_is_never_reported_as_done(monkeypatch, code):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    cancel_reply(monkeypatch, w, raise_code=code)
    heard = await _confirmed_cancel(p)
    assert cancels(w), "the confirmed cancellation was sent"
    assert len(w.square.active()) == 1 and w.store.live, "Square and CARA both still hold it"
    assert not any("has been cancelled" in line for line in heard), heard
    if code == "BOOKING_OUTCOME_UNKNOWN":
        assert p.session.entities["booking_reconciliation_required"]["operation"] == "cancel"


@pytest.mark.parametrize("reply", [
    {"id": "sq_1"},                                                   # thin: read back
    {"id": "other", "status": "CANCELLED_BY_CUSTOMER"},              # someone else's booking
    {"id": "sq_1", "status": "ACCEPTED"},                            # not cancelled
])
async def test_voice_cancel_reply_must_name_this_booking_cancelled(monkeypatch, reply):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    cancel_reply(monkeypatch, w, reply=reply)
    heard = await _confirmed_cancel(p)
    assert w.store.live, "CARA keeps the row while Square has not confirmed"
    assert not any("has been cancelled" in line for line in heard), heard
    assert p.session.entities.get("booking_reconciliation_required")


async def test_voice_cancel_thin_reply_that_reads_back_cancelled_is_done(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__

    async def thin(method, path, *, json=None):
        result = await original(method, path, json=json)
        if method == "POST" and path.endswith("/cancel"):
            return {"booking": {"id": result["booking"]["id"]}}
        return result

    monkeypatch.setattr(w.adapter.delegate, "_request", thin)
    heard = await _confirmed_cancel(p)
    assert not w.square.active() and not w.store.live
    assert any("cancelled" in line for line in heard), heard


async def test_legacy_cancel_square_refusal_keeps_the_row_and_says_so(monkeypatch):
    w = make_world(monkeypatch)
    c = await _legacy_booked(w, monkeypatch)
    cancel_reply(monkeypatch, w, raise_code="FORBIDDEN")
    await c.says("Please cancel my appointment.", intent=_intent("cancel", T2, w.first))
    said = await c.says("Yes.", intent=None, reply="Your appointment has been cancelled.")
    assert "has been cancelled" not in said.lower() and "still booked" in said
    assert len(w.square.active()) == 1 and w.store.live


# --------------------------------------------------------------------------- #
# R2-F6: a move is verified against everything the caller authorized
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("corruption", ["extra_segment", "staff", "pending_ok"])
async def test_move_reply_is_checked_segment_by_segment(monkeypatch, corruption):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__

    async def corrupt(method, path, *, json=None):
        result = await original(method, path, json=json)
        if method == "PUT":
            booking = w.square.bookings[path.rsplit("/", 1)[-1]]
            segment = booking["appointment_segments"][0]
            if corruption == "extra_segment":
                booking["appointment_segments"].append(dict(segment))
            elif corruption == "staff":
                segment["team_member_id"] = "TM_SOMEONE_ELSE"
            else:
                booking["status"] = "PENDING"   # awaiting the business's approval: still active
            return {"booking": copy.deepcopy(booking)}
        return result

    monkeypatch.setattr(w.adapter.delegate, "_request", corrupt)
    await p.says("Move my appointment to Thursday at 3 PM instead.")
    await p.propose(operation="reschedule", requested_start_iso=local(T3), service_description=w.first)
    await p.says("Yes, please.")
    flagged = bool(p.session.entities.get("booking_reconciliation_required"))
    assert flagged is (corruption != "pending_ok"), (corruption, p.session.booking_status)


# --------------------------------------------------------------------------- #
# R2-F7: a voice change only ever touches the caller's own appointment
# --------------------------------------------------------------------------- #
from app.services.booking_state import get_draft as _get_draft, save_draft as _save_draft  # noqa: E402
from app.services.call_state import CallSession  # noqa: E402


def _other_caller(world, sid="CA-other", phone="+15559990000"):
    return CallSession(sid, "inbound", phone, "+15550000001",
                       tenant_id=str(world.spa.id), timezone="America/Chicago")


async def test_contaminated_offer_cannot_cancel_another_callers_booking(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    await booked_call(w)
    victim = w.store.live[0]
    q = Phone(w, _other_caller(w))
    draft = _get_draft(q.session)
    draft.offered_appointment_ids = [str(victim.id)]
    draft.target_appointment_id = str(victim.id)
    _save_draft(q.session, draft)
    await q.says("Please cancel my appointment.")
    out = await q.tool("cancel_appointment", appointment_id=str(victim.id))
    assert out["status"] != "cancellation_confirmation_required", out
    await q.says("Yes.")
    await q.tool("cancel_appointment", appointment_id=str(victim.id))
    assert not cancels(w) and len(w.square.active()) == 1


async def test_same_caller_on_a_later_call_can_still_cancel_their_booking(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    await booked_call(w)
    own = w.store.live[0]
    later = Phone(w, _other_caller(w, sid="CA-later", phone="+15550000002"))
    draft = _get_draft(later.session)
    draft.offered_appointment_ids = [str(own.id)]
    _save_draft(later.session, draft)
    await later.says("Please cancel my appointment.")
    out = await later.tool("cancel_appointment", appointment_id=str(own.id))
    assert out["status"] == "cancellation_confirmation_required", out
    await later.says("Yes.")
    assert len(cancels(w)) == 1 and not w.square.active()


# --------------------------------------------------------------------------- #
# R2-F8: an unresolved change blocks every later change, from anywhere
# --------------------------------------------------------------------------- #
async def _lost_move_on_sqlite(rubyz_db, monkeypatch):
    from app.services import appointment_booking_service as booking
    from app.services.booking_state import arm_verified_proposal
    from app.services.grok_service import AppointmentIntent

    db, a, _b, squares = await setup_tenants(rubyz_db, monkeypatch)
    call1 = session_for(a, "lost-move")
    row = await book(db, a, call1)
    row_id = row.id
    square = squares["fake-cleaning-token"]
    square.open_all(T3)

    async def lose(method, path, *, json=None):
        result = await square(method, path, json=json)
        if method == "PUT":
            raise BookingProviderError("response lost", code="BOOKING_OUTCOME_UNKNOWN", retryable=False)
        return result

    squares["fake-cleaning-token"] = lose
    call1.add_turn("user", "Move my appointment to Thursday at 3 PM instead.")
    await booking.stage_booking(db, call1, AppointmentIntent(
        intent="reschedule", confidence=1, service_description=a.services[0]["name"],
        requested_start_iso=local(T3)))
    arm_verified_proposal(call1)
    assert (await booking.confirm_booking(db, call1)).outcome is booking.BookingOutcome.ERROR
    squares["fake-cleaning-token"] = square
    await db.refresh(a)   # the real rollback expires ORM objects
    return db, a, square, row_id


async def test_unresolved_change_is_durable_and_blocks_the_dashboard(rubyz_db, monkeypatch):
    from sqlalchemy import select
    from app.api.v1.appointments import cancel_appointment
    from app.models.booking_escalation import BookingEscalation

    db, a, square, row_id = await _lost_move_on_sqlite(rubyz_db, monkeypatch)
    records = list((await db.execute(select(BookingEscalation))).scalars())
    assert [r.category for r in records] == ["unresolved_change"]
    assert records[0].details["appointment_id"] == str(row_id) and records[0].status == "pending"
    writes = len([c for c in square.calls if c[0] == "PUT" or c[1].endswith("/cancel")])
    with pytest.raises(HTTPException) as raised:
        await cancel_appointment(row_id, scope=session_for(a, "staff").scope, db=db)
    assert raised.value.status_code == 409
    assert len([c for c in square.calls if c[0] == "PUT" or c[1].endswith("/cancel")]) == writes
    # Once staff have reconciled it, changes are possible again.
    records[0].status = "resolved"
    await db.commit()
    await cancel_appointment(row_id, scope=session_for(a, "staff").scope, db=db)
    assert not square.active()


async def test_unresolved_change_blocks_a_later_voice_call_even_after_its_question(rubyz_db, monkeypatch):
    from app.services import appointment_booking_service as booking

    db, a, square, row_id = await _lost_move_on_sqlite(rubyz_db, monkeypatch)
    later = session_for(a, "later-call")
    later.add_turn("user", "Please cancel my appointment.")
    first = await booking.cancel_booking(db, later)
    later.add_turn("user", "Yes.")
    second = await booking.cancel_booking(db, later)
    assert first.outcome is booking.BookingOutcome.ERROR, first.message   # blocked before asking
    assert second.outcome is not booking.BookingOutcome.CANCELLED, second.message
    assert later.entities.get("booking_reconciliation_required", {}).get("appointment_id") == str(row_id)
    assert not any(c[1].endswith("/cancel") for c in square.calls)
