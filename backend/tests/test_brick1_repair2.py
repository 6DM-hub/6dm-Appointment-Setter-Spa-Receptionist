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
])
async def test_explicit_cancel_request_still_cancels(monkeypatch, request_text):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says(request_text)
    out = await p.tool("cancel_appointment")
    assert out["status"] == "cancelled", out
    assert len(cancels(w)) == 1 and not w.square.active()


async def test_cancel_request_then_yes_to_confirmation_cancels(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("I'd like to cancel my appointment.")
    p.session.add_turn("assistant", "Just to confirm, cancel your Thursday 2 PM appointment?")
    await p.says("Yes.")
    assert (await p.tool("cancel_appointment"))["status"] == "cancelled"
    assert len(cancels(w)) == 1


@pytest.mark.parametrize("answer", [
    "Actually, never mind.", "No, don't cancel it.", "No.", "Keep it.",
    "Cancel it... actually never mind.",
])
async def test_withdrawn_cancellation_cancels_nothing_and_says_so(monkeypatch, answer):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("I want to cancel my appointment.")
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
    await c.says("Please cancel my appointment.", intent=_intent("cancel", T2, w.first),
                 reply="Your appointment is cancelled. Anything else?")
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
])
async def test_real_consent_still_books(monkeypatch, consent):
    w = make_world(monkeypatch)
    p = await _offered(w)
    await p.says(consent)
    assert p.starts() == [Z2] and len(w.square.creates) == 1, (consent, p.heard)


@pytest.mark.parametrize("utterance", [
    "Yes, what's the cancellation policy?", "Yes, is there parking?", "Yes, hold on a second.",
    "Yeah, I'm driving right now.", "Sure, what do I need to bring?",
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
    await p.says("Yes.")
    await p.tool("confirm_appointment")
    assert not w.square.creates
    # Asked about the booking again, the same "yes" books it.
    p.session.add_turn("assistant", "Would you like me to book Thursday at 2 PM?")
    await p.says("Yes, please.")
    assert p.starts() == [Z2] and len(w.square.creates) == 1


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
    out = await p.tool("cancel_appointment")
    assert out["status"] == "error"
    assert p.session.entities["booking_reconciliation_required"]["operation"] == "cancel"
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
