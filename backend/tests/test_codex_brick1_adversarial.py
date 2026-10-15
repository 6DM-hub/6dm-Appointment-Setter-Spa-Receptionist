"""Independent adversarial verification. Only provider transport is simulated.

Safety assertions deliberately fail when the reviewed application violates
the user's requirements. No production code is altered by this module.
"""
import asyncio
import copy
import json
from datetime import timedelta

import pytest

from app.models import AppointmentStatus
from app.services import appointment_booking_service as engine
from app.services.booking_state import get_draft
from app.services.booking_adapters.base import BookingProviderError
from tests.test_brick1_booking_change_guard import (
    Phone, make_world, booked_call, reconnect, _legacy_booked, _intent,
    T2, T3, T4, T5, Z2, Z3, Z4, Z5, local,
)
from tests.square_world import call


def cancellation_transport(monkeypatch, world):
    """Add Square's cancellation endpoint omitted by the supplied fake."""
    original = world.square.__call__
    async def request(method, path, *, json=None):
        if method == "POST" and path.endswith("/cancel"):
            body = copy.deepcopy(json or {})
            world.square.calls.append((method, path, body))
            booking = world.square.bookings[path.split("/")[-2]]
            assert body["booking_version"] == booking["version"]
            booking["status"] = "CANCELLED_BY_CUSTOMER"
            booking["version"] += 1
            return {"booking": copy.deepcopy(booking)}
        return await original(method, path, json=json)
    monkeypatch.setattr(world.adapter.delegate, "_request", request)


@pytest.mark.parametrize("phrase", ["Never mind.", "Keep it.", "Don't change it.", "Leave my appointment alone."])
@pytest.mark.parametrize("pending", [False, True], ids=["completed", "change_pending"])
async def test_delayed_cancel_tool_after_keep_must_not_cancel(monkeypatch, phrase, pending):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    if pending:
        await p.says("Move my appointment to Thursday at 3 PM instead.")
        assert (await p.propose(operation="reschedule", requested_start_iso=local(T3),
                                service_description=w.first))["status"] == "draft"
    await p.says(phrase)
    out = await p.tool("cancel_appointment")
    cancellations = [x for x in w.square.calls if x[1].endswith("/cancel")]
    assert not cancellations, ("UNAUTHORIZED CANCELLATION", phrase, out, cancellations)
    assert p.starts() == [Z2]


async def test_engine_cancel_after_keep_must_not_bypass_guard(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    p = await booked_call(w)
    await p.says("Keep it.")
    result = await engine.cancel_booking(w.db, p.session)
    assert p.starts() == [Z2], (result.outcome, w.square.calls[-2:])


@pytest.mark.parametrize("utterance", ["Yes, where are you located?", "Yes, can you repeat that?", "Yes, I can hear you."])
@pytest.mark.parametrize("path", ["realtime", "legacy"])
async def test_unrelated_yes_is_not_booking_consent(monkeypatch, utterance, path):
    w = make_world(monkeypatch)
    if path == "realtime":
        p = Phone(w)
        await p.says(f"A {w.first} Thursday at 2 PM. My name is Ada Lovelace.")
        assert (await p.propose(operation="schedule", requested_start_iso=local(T2),
                                service_description=w.first, caller_name="Ada Lovelace"))["status"] == "draft"
        await p.says(utterance)
    else:
        from tests.test_brick1_booking_change_guard import LegacyCall
        p = LegacyCall(w, monkeypatch)
        await p.says(f"A {w.first} Thursday at 2 PM. My name is Ada Lovelace.",
                     intent=_intent("schedule", T2, w.first),
                     reply="Thursday at 2 PM is available. Would you like me to book that?")
        await p.says(utterance, reply="Our address is on Main Street.")
    assert not w.square.creates, ("UNAUTHORIZED CREATE", utterance, w.square.creates)


async def test_staff_only_change_must_reach_square(monkeypatch):
    w = make_world(monkeypatch)
    w.square.openings = [slot for slot in w.square.openings
                         if not (slot["start"] == T2 and slot["team"] == "TM_BEA")]
    p = await booked_call(w)
    assert w.square.active()[0]["appointment_segments"][0]["team_member_id"] == "TM_ANA"
    await p.says("Change my appointment to be with Bea on Thursday at 3 PM instead.")
    out = await p.propose(operation="reschedule", requested_start_iso=local(T3),
                          service_description=w.first, preferred_staff="Bea")
    assert out["status"] == "draft", out
    await p.says("Yes, please move it.")
    # 'move' is intentionally an edit word; follow with pure consent.
    await p.says("Yes, go ahead.")
    assert w.square.active()[0]["appointment_segments"][0]["team_member_id"] == "TM_BEA", (
        "CONFIRMED STAFF CHANGE NOT APPLIED", p.heard, p.outputs, w.square.active())


@pytest.mark.parametrize("phase", ["before_booking", "after_completed"])
async def test_missing_or_out_of_order_confirmation_is_safe(monkeypatch, phase):
    w = make_world(monkeypatch)
    p = Phone(w) if phase == "before_booking" else await booked_call(w)
    before = copy.deepcopy(w.square.bookings)
    await p.tool("confirm_appointment")
    await p.tool("manage_appointment", intent="schedule", requested_start_iso=local(T5))
    assert w.square.bookings == before


@pytest.mark.parametrize("clock", ["3 PM works", "5 PM works"])
async def test_clock_acceptance_cannot_confirm_unoffered_time(monkeypatch, clock):
    w = make_world(monkeypatch)
    p = Phone(w)
    await p.says(f"A {w.first} Thursday at 2 PM. My name is Ada Lovelace.")
    assert (await p.propose(operation="schedule", requested_start_iso=local(T2),
                            service_description=w.first, caller_name="Ada Lovelace"))["status"] == "draft"
    await p.says(clock)
    await p.tool("confirm_appointment")
    assert not w.square.creates, (clock, p.heard, p.outputs)


async def test_negative_answer_then_unrelated_yes_must_not_resurrect_offer(monkeypatch):
    w = make_world(monkeypatch)
    p = Phone(w)
    await p.says(f"A {w.first} Thursday at 2 PM. My name is Ada Lovelace.")
    await p.propose(operation="schedule", requested_start_iso=local(T2),
                    service_description=w.first, caller_name="Ada Lovelace")
    await p.says("No, don't book that.")
    p.session.add_turn("assistant", "Would you like our address?")
    await p.says("Yes.")
    assert not w.square.creates, ("REJECTED OFFER RESURRECTED", p.heard, w.square.creates)


async def test_lost_update_response_then_keep_must_not_claim_old_time(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__
    async def lost_response(method, path, *, json=None):
        result = await original(method, path, json=json)
        if method == "PUT":
            raise asyncio.TimeoutError("provider applied update; response was lost")
        return result
    monkeypatch.setattr(w.adapter.delegate, "_request", lost_response)
    await p.says("Move my appointment to Thursday at 3 PM instead.")
    assert (await p.propose(operation="reschedule", requested_start_iso=local(T3),
                            service_description=w.first))["status"] == "draft"
    await p.says("Yes, go ahead.")
    assert p.starts() == [Z3], "fault injection must actually apply the update"
    assert len(w.square.updates) == 1
    heard_before = len(p.heard)
    await p.says("Never mind, keep it.")
    said = p.heard[heard_before:]
    assert not any("2 PM" in x and "stays exactly" in x for x in said), (
        "FALSE OLD-BOOKING ASSURANCE AFTER LOST RESPONSE", said, p.starts())
    assert p.session.entities.get("booking_reconciliation_required"), (
        "UNCERTAIN UPDATE MUST REQUIRE RECONCILIATION", p.session.booking_status, said)


@pytest.mark.parametrize("business,first,second", [
    ("cleaning", "House Cleaning", "Deep Cleaning"),
    ("roofing", "Roofing Estimate", "Roof Inspection"),
    ("construction", "Construction Site Visit", "Remodel Consultation"),
    ("pool", "Pool Cleaning", "Pool Inspection"),
    ("hvac_extra", "HVAC Service Visit", "Duct Inspection"),
])
async def test_generic_industry_additional_preserves_original(monkeypatch, business, first, second):
    from tests.test_brick1_booking_change_guard import BUSINESSES
    menu = [dict(name=name, duration_minutes=60, square_variation_id=f"var_{business}_{i}",
                 square_variation_version=1) for i, name in enumerate([first, second])]
    monkeypatch.setitem(BUSINESSES, business, menu)
    w = make_world(monkeypatch, business)
    p = await booked_call(w)
    original = copy.deepcopy(w.square.active()[0])
    await p.says(f"I also want to book another appointment, a {second} Thursday at 4 PM.")
    out = await p.propose(operation="schedule", requested_start_iso=local(T4), service_description=second,
                          caller_name="Ada Lovelace")
    assert out["status"] == "draft", out
    assert any("additional appointment" in x for x in p.heard)
    await p.says("Yes, go ahead.")
    assert p.starts() == [Z2, Z4], (p.heard, p.outputs)
    assert w.square.bookings[original["id"]] == original
    assert len(w.square.creates) == 2 and not w.square.updates


async def test_reconnect_then_keep_blocks_delayed_proposal(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    await p.says("Move my appointment to Thursday at 3 PM instead.")
    await p.propose(operation="reschedule", requested_start_iso=local(T3), service_description=w.first)
    p = reconnect(p)
    await p.says("Don't change it.")
    await p.propose(operation="reschedule", requested_start_iso=local(T3), service_description=w.first)
    await p.tool("confirm_appointment")
    await p.says("Yes, go ahead.")
    assert p.starts() == [Z2] and not w.square.updates


@pytest.mark.parametrize("field", ["start", "service", "staff", "customer", "location", "duration"])
async def test_create_response_must_match_the_exact_authorized_offer(monkeypatch, field):
    w = make_world(monkeypatch)
    p = Phone(w)
    await p.says(f"A {w.first} Thursday at 2 PM. My name is Ada Lovelace.")
    out = await p.propose(operation="schedule", requested_start_iso=local(T2),
                          service_description=w.first, caller_name="Ada Lovelace")
    assert out["status"] == "draft"
    original = w.square.__call__
    async def mismatch(method, path, *, json=None):
        result = await original(method, path, json=json)
        if method == "POST" and path == "/v2/bookings":
            booking = w.square.bookings[result["booking"]["id"]]
            if field == "start": booking["start_at"] = Z5
            elif field == "service": booking["appointment_segments"][0]["service_variation_id"] = "var_facial_60"
            elif field == "staff": booking["appointment_segments"][0]["team_member_id"] = "TM_UNAUTHORIZED"
            elif field == "customer": booking["customer_id"] = "cust_somebody_else"
            elif field == "location": booking["location_id"] = "loc_another_business"
            elif field == "duration": booking["appointment_segments"][0]["duration_minutes"] = 90
            return {"booking": copy.deepcopy(booking)}
        return result
    monkeypatch.setattr(w.adapter.delegate, "_request", mismatch)
    await p.says("Yes, go ahead.")
    assert len(w.square.creates) == 1, "fault injection must reach real adapter create"
    assert p.session.booking_status != "booked", (
        "MISMATCHED PROVIDER RESULT REPORTED AS BOOKED", field, p.heard, w.square.active())
    assert p.session.entities.get("booking_reconciliation_required")


async def test_legacy_same_call_keep_overrides_erroneous_cancel_intent(monkeypatch):
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    c = await _legacy_booked(w, monkeypatch)
    await c.says("Don't change it.", intent=_intent("cancel", T2, w.first))
    assert c.starts() == [Z2]
    assert not any(path.endswith("/cancel") for _, path, _ in w.square.calls)


async def test_legacy_prior_call_keep_overrides_erroneous_cancel_intent(monkeypatch):
    from tests.test_brick1_booking_change_guard import LegacyCall
    w = make_world(monkeypatch)
    cancellation_transport(monkeypatch, w)
    await booked_call(w)
    c = LegacyCall(w, monkeypatch)
    async def contact(*args, **kwargs): return w.contact
    async def upcoming(*args, **kwargs): return w.store.live
    monkeypatch.setattr(engine, "_find_contact_by_phone", contact)
    monkeypatch.setattr(engine, "_find_upcoming_appointments", upcoming)
    await c.says("Leave my appointment alone.", intent=_intent("cancel", T2, w.first))
    assert c.starts() == [Z2], (c.replies, w.square.calls[-2:])


async def test_provider_rejects_update_no_false_success_no_duplicate(monkeypatch):
    w = make_world(monkeypatch)
    p = await booked_call(w)
    original = w.square.__call__
    async def rejected(method, path, *, json=None):
        if method == "PUT":
            raise BookingProviderError("provider rejected stale version", code="CONFLICT")
        return await original(method, path, json=json)
    monkeypatch.setattr(w.adapter.delegate, "_request", rejected)
    await p.says("Move my appointment to Thursday at 3 PM instead.")
    await p.propose(operation="reschedule", requested_start_iso=local(T3), service_description=w.first)
    before = len(p.heard)
    await p.says("Yes, go ahead.")
    assert p.starts() == [Z2] and len(w.square.creates) == 1 and not w.square.updates
    assert not any("You're all set" in x for x in p.heard[before:])


async def test_simultaneous_calls_keep_drafts_and_writes_separate(monkeypatch):
    w = make_world(monkeypatch)
    p = Phone(w, call(w.spa, sid="CA-concurrent-a"))
    q = Phone(w, call(w.spa, sid="CA-concurrent-b"))
    await asyncio.gather(
        p.says(f"A {w.first} Thursday at 2 PM. My name is Ada Lovelace."),
        q.says(f"A {w.second} Thursday at 4 PM. My name is Ada Lovelace."))
    results = await asyncio.gather(
        p.propose(operation="schedule", requested_start_iso=local(T2), service_description=w.first,
                  caller_name="Ada Lovelace"),
        q.propose(operation="schedule", requested_start_iso=local(T4), service_description=w.second,
                  caller_name="Ada Lovelace"))
    assert all(x["status"] == "draft" for x in results), results
    assert get_draft(p.session).booking_id != get_draft(q.session).booking_id
    await asyncio.gather(p.says("Yes, go ahead."), q.says("Yes, go ahead."))
    assert sorted(b["start_at"] for b in w.square.active()) == [Z2, Z4]
    assert len(w.square.creates) == 2 and not w.square.updates
    assert p.session.appointment_id != q.session.appointment_id


async def test_replayed_same_tool_id_with_different_arguments_is_not_a_second_proposal(monkeypatch):
    w = make_world(monkeypatch)
    p = Phone(w)
    await p.says(f"A {w.first} Thursday at 2 PM. My name is Ada Lovelace.")
    async def dispatch(start):
        await p.voice._handle_function_call({"name": "propose_appointment", "call_id": "replayed-id",
            "arguments": json.dumps(dict(operation="schedule", requested_start_iso=local(start),
                                         service_description=w.first, caller_name="Ada Lovelace"))})
    await dispatch(T2)
    before = get_draft(p.session).to_dict()
    await dispatch(T5)
    assert get_draft(p.session).to_dict() == before
    await p.says("Yes, go ahead.")
    assert p.starts() == [Z2] and len(w.square.creates) == 1


@pytest.mark.parametrize("business,first,second", [
    ("cleaning", "House Cleaning", "Deep Cleaning"),
    ("roofing", "Roofing Estimate", "Roof Inspection"),
    ("construction", "Construction Site Visit", "Remodel Consultation"),
    ("pool", "Pool Cleaning", "Pool Inspection"),
    ("hvac", "AC Tune-Up", "Furnace Inspection"),
])
async def test_industry_service_correction_is_not_consent_to_old_service(monkeypatch, business, first, second):
    from tests.test_brick1_booking_change_guard import BUSINESSES
    menu = [dict(name=name, duration_minutes=60, square_variation_id=f"var_correction_{business}_{i}",
                 square_variation_version=1) for i, name in enumerate([first, second])]
    monkeypatch.setitem(BUSINESSES, business, menu)
    w = make_world(monkeypatch, business)
    p = Phone(w)
    await p.says(f"A {first} Thursday at 2 PM. My name is Ada Lovelace.")
    assert (await p.propose(operation="schedule", requested_start_iso=local(T2),
                            service_description=first, caller_name="Ada Lovelace"))["status"] == "draft"
    await p.says(f"Yes, {second}.")
    assert not w.square.creates, ("SERVICE CORRECTION BOOKED OLD SERVICE", second, w.square.creates)
