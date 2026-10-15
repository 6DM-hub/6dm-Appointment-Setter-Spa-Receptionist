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
