"""Google event-ID contract with simulated transport, including real voice keys."""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from app.services import google_calendar as gc
from tests.test_brick1_google_reply_validation import (
    _google_world, _realtime_booked, _legacy_booked_google, _the_appointment, T2,
)


@pytest.mark.parametrize("path", ["realtime", "legacy"])
@pytest.mark.parametrize("business", ["spa", "hvac", "dental"])
async def test_voice_intent_key_becomes_valid_google_id(monkeypatch, path, business):
    world = _google_world(monkeypatch, business, "valid")
    await (_realtime_booked(world) if path == "realtime" else _legacy_booked_google(world))
    body = world.google.inserts[0]["body"]
    assert re.fullmatch(r"[a-v0-9]{5,1024}", body["id"]), body["id"]
    assert _the_appointment(world).external_booking_id == body["id"]


async def test_retry_stability_distinct_keys_and_legacy_uuid_compatibility(monkeypatch):
    sent = []
    def insert(**kwargs):
        sent.append(kwargs["body"])
        return SimpleNamespace(execute=lambda: {"id": kwargs["body"]["id"]})
    async def service(_):
        return SimpleNamespace(events=lambda: SimpleNamespace(insert=insert))
    monkeypatch.setattr(gc, "_service", service)
    connection = SimpleNamespace(selected_calendar_id="simulated-calendar")
    async def create(key):
        return await gc.create_event(connection, title="Inspection", start=T2,
            end=T2 + timedelta(hours=1), timezone_name="UTC", description="",
            event_id=key)
    key = "CA0123456789abcdef:01234567-89ab-cdef-0123-456789abcdef"
    first = await create(key)
    assert re.fullmatch(r"[a-v0-9]{5,1024}", first), first
    assert await create(key) == first
    assert await create(key + "different") != first
    legacy = "01234567-89ab-cdef-0123-456789abcdef"
    assert await create(legacy) == legacy.replace("-", "")
