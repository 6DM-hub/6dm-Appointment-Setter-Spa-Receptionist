"""Owner contract: verified PENDING is received, never confirmed."""
import re
import pytest

from tests.test_brick1_booking_change_guard import make_world, LegacyCall, _intent, T2
from tests.test_brick1_google_g2_pending_create import _staged_phone


@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("industry", ["spa", "hvac", "dental"])
@pytest.mark.parametrize("path", ["realtime", "legacy"])
@pytest.mark.parametrize("status", ["PENDING", "ACCEPTED", "DECLINED"])
async def test_square_status_controls_local_outcome_and_speech(monkeypatch, industry, path, status, replay):
    world = make_world(monkeypatch, industry)
    delegate = world.adapter.delegate
    original = delegate._request
    sent = []

    async def request(method, route, **kwargs):
        result = await original(method, route, **kwargs)
        if method == "POST" and route == "/v2/bookings":
            import copy
            sent.append(copy.deepcopy(kwargs["json"]))
            result["booking"]["status"] = status
            world.square.bookings[result["booking"]["id"]]["status"] = status
            if replay and len(sent) == 1:
                from app.services.booking_adapters.base import BookingProviderError
                raise BookingProviderError("Lost create reply", code="BOOKING_OUTCOME_UNKNOWN")
        return result

    monkeypatch.setattr(delegate, "_request", request)
    if path == "realtime":
        phone = await _staged_phone(world)
        said = await phone.says("Yes, please.")
    else:
        phone = LegacyCall(world, monkeypatch)
        await phone.says(f"I'd like a {world.first} Thursday at 2 PM, I'm Ada Lovelace.",
                         intent=_intent("schedule", T2, world.first), reply="Thursday at 2 PM is open. Would you like me to book that?")
        said = [await phone.says("Yes.", reply="You're all set. You're booked.")]
    rows = list(world.store.rows)
    if status == "DECLINED":
        assert rows == []
        return
    assert len(rows) == 1, "VERIFIED SQUARE REQUEST HAS NO LOCAL APPOINTMENT"
    assert rows[0].external_booking_id in world.square.bookings
    assert len(world.square.bookings) == 1
    assert len(sent) == (2 if replay else 1)
    assert all(payload == sent[0] for payload in sent)
    assert not phone.session.entities.get("booking_reconciliation_required")
    if status == "PENDING":
        assert rows[0].status == "awaiting_provider_approval"
        speech = " ".join(said).lower()
        assert "request" in speech and ("confirm" in speech or "approval" in speech)
        assert not re.search(r"you(?:'re| are) (?:all set|booked|confirmed)", speech)


@pytest.mark.parametrize("field", ["customer_id", "location_id", "start_at"])
async def test_pending_create_mismatch_never_becomes_local_request(monkeypatch, field):
    world = make_world(monkeypatch, "spa")
    delegate = world.adapter.delegate
    original = delegate._request

    async def request(method, route, **kwargs):
        result = await original(method, route, **kwargs)
        if method == "POST" and route == "/v2/bookings":
            result["booking"]["status"] = "PENDING"
            result["booking"][field] = "2035-01-01T12:00:00Z" if field == "start_at" else "other"
            world.square.bookings[result["booking"]["id"]].update(result["booking"])
        return result

    monkeypatch.setattr(delegate, "_request", request)
    phone = await _staged_phone(world)
    await phone.says("Yes, please.")
    assert list(world.store.rows) == [], "MISMATCHED PENDING REQUEST ACCEPTED"
    assert phone.session.entities.get("booking_reconciliation_required")
