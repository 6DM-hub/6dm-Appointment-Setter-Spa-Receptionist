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
