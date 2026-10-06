"""TwilioService.set_inbound_status_callback.

Regression coverage for the inbound transcript bug: Twilio never posts to
`/voice/status` for an inbound call unless something attaches a
StatusCallback to it, so `voice_status` (and the `call_log.transcript` write
it performs) never fired. `set_inbound_status_callback` closes that gap by
updating the live Call resource right after the call is created.
"""
import pytest

from app.core.config import settings
from app.services.twilio_service import TwilioService


@pytest.mark.asyncio
async def test_sets_status_callback_on_the_live_call(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://example.ngrok.dev")
    service = TwilioService.__new__(TwilioService)

    calls = {}

    class FakeCallContext:
        def update(self, **kwargs):
            calls["kwargs"] = kwargs

    def fake_calls(call_sid):
        calls["call_sid"] = call_sid
        return FakeCallContext()

    class FakeClient:
        calls = staticmethod(fake_calls)

    service._client = FakeClient()

    await service.set_inbound_status_callback("CAtest123")

    assert calls["call_sid"] == "CAtest123"
    assert calls["kwargs"]["status_callback"] == (
        f"https://example.ngrok.dev{settings.API_V1_PREFIX}/telephony/voice/status"
    )
    assert calls["kwargs"]["status_callback_method"] == "POST"


@pytest.mark.asyncio
async def test_failure_to_attach_callback_does_not_raise(monkeypatch):
    """Inbound call handling must not fail just because this best-effort
    Twilio update failed (rate limit, network blip, etc.)."""
    service = TwilioService.__new__(TwilioService)

    class FakeCallContext:
        def update(self, **kwargs):
            raise RuntimeError("boom")

    class FakeClient:
        calls = staticmethod(lambda call_sid: FakeCallContext())

    service._client = FakeClient()

    await service.set_inbound_status_callback("CAtest123")
