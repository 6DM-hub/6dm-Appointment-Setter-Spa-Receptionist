import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api.v1.telephony import voice_outbound_answer
from app.core.config import settings
from app.services.call_state import CallSession
from app.services.media_bridge import TwilioMediaBridge
from app.services import voice_setup


def outbound_session():
    return CallSession(call_sid="CAoutbound", direction="outbound", from_number="+15550000001",
        to_number="+15550000002", business_name="6DM", timezone="America/Chicago",
        call_objective="Offer a presentation for the salon owner.", entities={"voice_engine": "xai_realtime"})


async def test_outbound_answer_streams_without_text_to_speech(monkeypatch):
    from app.api.v1 import telephony
    session = outbound_session()
    state = AsyncMock()
    state.get.return_value = session
    text_generator = AsyncMock()
    monkeypatch.setattr(telephony.grok_service, "generate_voice_response", text_generator)
    response = await voice_outbound_answer(CallSid=session.call_sid, state=state)
    xml = response.body.decode()
    assert "<Connect>" in xml and "media-stream/CAoutbound" in xml
    assert "<Say" not in xml and "<Gather" not in xml
    assert "Cara" in session.history[0]["content"]
    assert "thank you for calling" not in session.history[0]["content"].lower()
    text_generator.assert_not_awaited()
    state.save.assert_awaited_once()


async def test_realtime_outbound_refuses_missing_session(monkeypatch):
    monkeypatch.setattr(settings, "XAI_REALTIME_ENABLED", True)
    state = AsyncMock()
    state.get.return_value = None
    with pytest.raises(HTTPException) as error:
        await voice_outbound_answer(CallSid="CAmissing", state=state)
    assert error.value.status_code == 503


async def test_outbound_bridge_uses_sales_objective_and_ara(monkeypatch):
    monkeypatch.setattr(settings, "XAI_VOICE_ID", "ara")
    bridge = TwilioMediaBridge("CAoutbound", outbound_session(), None)
    bridge._send = AsyncMock()
    await bridge._configure()
    config = bridge._send.call_args.args[0]["session"]
    assert config["voice"] == "ara"
    assert "outbound B2B" in config["instructions"]
    assert "salon owner" in config["instructions"]
    assert "never to a spa calendar" in config["instructions"]
    assert {tool["name"] for tool in config["tools"]} == {
        "check_availability", "propose_appointment", "confirm_appointment",
    }
    assert config["audio"]["input"]["format"]["type"] == "audio/pcmu"
    assert config["audio"]["output"]["format"]["type"] == "audio/pcmu"


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_deployment_probe_requires_configured_audio(direction, monkeypatch):
    class Socket:
        def __init__(self):
            self.sent = []
            self.events = iter([
                {"type": "session.created"}, {"type": "session.updated"},
                {"type": "response.audio.delta", "delta": "/w=="}, {"type": "response.done"},
            ])
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        async def send(self, frame):
            self.sent.append(json.loads(frame))
        async def recv(self):
            return json.dumps(next(self.events))
    socket = Socket()
    monkeypatch.setattr(voice_setup, "connect", lambda *args, **kwargs: socket)
    result = await voice_setup.probe_audio(direction)
    assert result["status"] == "ready" and result["audio_bytes"] == 1
    assert socket.sent[0]["type"] == "session.update"
    assert socket.sent[0]["session"]["audio"]["output"]["format"]["type"] == "audio/pcmu"
    assert socket.sent[-1]["type"] == "response.create"
