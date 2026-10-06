"""Tests for the Twilio Media Stream <-> xAI realtime audio bridge.

A live call cannot be simulated here, so these pin the protocol translation,
which is where a silent failure would live: audio must be relayed verbatim in
both directions, playback must be cleared when the caller interrupts, frames
must carry the streamSid Twilio assigned, and the per-spa persona and μ-law
format must reach `session.update`.
"""
import asyncio
import base64
import json
import uuid

import pytest

from app.core.config import settings
from app.services.call_state import CallSession
from app.services.media_bridge import (
    MULAW_FRAME_BYTES,
    TwilioMediaBridge,
    mulaw_silence,
)


class _FakeTwilioWS:
    """Stands in for Starlette's WebSocket on the Twilio side."""

    def __init__(self, inbound: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._inbound = list(inbound or [])

    async def send_text(self, text: str) -> None:
        # A real socket yields to the event loop on every send. Without this
        # the fake completes each send synchronously, so concurrent writers
        # could never interleave here and the ordering test would pass even
        # against the bug it exists to catch.
        await asyncio.sleep(0)
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        if not self._inbound:
            return json.dumps({"event": "stop"})
        return json.dumps(self._inbound.pop(0))


def _session() -> CallSession:
    s = CallSession(
        call_sid="CAbridge",
        direction="inbound",
        from_number="+12149702434",
        to_number="+18058878345",
        business_name="Healing Waters Day Spa",
        tenant_prompt="SERVICE MENU:\n- Swedish Massage 60 minutes",
        tenant_id=str(uuid.uuid4()),
    )
    s.add_turn("assistant", "Thank you for calling Healing Waters Day Spa.")
    return s


@pytest.fixture
def bridge(monkeypatch):
    twilio = _FakeTwilioWS()
    b = TwilioMediaBridge("CAbridge", _session(), twilio)
    b._stream_sid = "MZ1234567890"

    sent_to_xai: list[dict] = []

    async def _send(payload):
        sent_to_xai.append(payload)

    async def _noop():
        return None

    b._send = _send
    b._persist_session = _noop
    b.xai_sent = sent_to_xai  # type: ignore[attr-defined]
    b.twilio = twilio  # type: ignore[attr-defined]
    return b


# --------------------------------------------------------------------------- #
# Session configuration
# --------------------------------------------------------------------------- #
async def test_session_update_requests_mulaw_both_ways(bridge):
    """Twilio sends G.711 μ-law; asking xAI for anything else would require
    transcoding and produce noise instead of speech."""
    await bridge._configure()
    session = bridge.xai_sent[0]["session"]
    assert session["audio"]["input"]["format"]["type"] == "audio/pcmu"
    assert session["audio"]["output"]["format"]["type"] == "audio/pcmu"


async def test_session_update_carries_the_tenant_persona(bridge):
    """The whole point of the bridge over the console agent: each spa's own
    prompt reaches the model."""
    await bridge._configure()
    session = bridge.xai_sent[0]["session"]
    assert "Healing Waters Day Spa" in session["instructions"]
    assert "SERVICE MENU:" in session["instructions"]
    assert session["voice"] == settings.XAI_VOICE_ID
    assert session["turn_detection"] == {"type": "server_vad"}
    # The bridge must offer the same propose/confirm split as the SIP path, or
    # the model on this path would still be able to book on hearing a date.
    names = [t["name"] for t in session["tools"]]
    assert names == [
        "lookup_spa_facts",
        "check_availability",
        "propose_appointment",
        "confirm_appointment",
        "lookup_appointments",
        "cancel_appointment",
        "start_new_appointment",
        "request_callback",
    ]
    # The only tool that writes must not accept a date, so it can only ever
    # commit the current pending request.
    confirm = next(t for t in session["tools"] if t["name"] == "confirm_appointment")
    assert confirm["parameters"]["properties"] == {}


async def test_bridge_url_has_no_call_id(bridge):
    """A plain realtime session needs no xAI-side call registration — that is
    what makes this work without the Voice Agent entitlement."""
    assert "call_id" not in bridge._url


# --------------------------------------------------------------------------- #
# Audio relay
# --------------------------------------------------------------------------- #
async def test_agent_audio_is_queued_as_one_ordered_delta(bridge):
    """xAI already emits ordered μ-law chunks. Splitting them into 20ms frames
    added queue delay without changing what Twilio plays, so each delta is one
    media message, in arrival order, with every sample preserved."""
    await bridge._dispatch(
        {"type": "response.output_audio.delta", "delta": mulaw_silence(5)}
    )
    assert len(bridge.twilio.sent) == 1
    frame = bridge.twilio.sent[0]
    assert frame["event"] == "media"
    assert frame["streamSid"] == "MZ1234567890"
    assert len(base64.b64decode(frame["media"]["payload"])) == 800


async def test_audio_bytes_are_preserved_exactly_when_queued(bridge):
    """A delta must not be altered, reordered, or dropped."""
    raw = bytes(range(256)) * 3  # 768 bytes: not a clean multiple of 160
    await bridge._dispatch(
        {
            "type": "response.output_audio.delta",
            "delta": base64.b64encode(raw).decode(),
        }
    )
    rebuilt = b"".join(
        base64.b64decode(f["media"]["payload"]) for f in bridge.twilio.sent
    )
    assert rebuilt == raw
    assert len(base64.b64decode(bridge.twilio.sent[-1]["media"]["payload"])) == 768


async def test_undecodable_audio_delta_is_dropped_not_raised(bridge):
    await bridge._dispatch(
        {"type": "response.output_audio.delta", "delta": "!!!not base64!!!"}
    )
    assert bridge.twilio.sent == []


@pytest.mark.parametrize(
    "etype", ["response.output_audio.delta", "response.audio.delta"]
)
async def test_both_audio_event_spellings_are_relayed(bridge, etype):
    await bridge._dispatch({"type": etype, "delta": mulaw_silence()})
    assert len(bridge.twilio.sent) == 1


async def test_audio_is_dropped_before_stream_sid_is_known(bridge):
    """Twilio rejects media frames without a streamSid; sending them early
    would error rather than play."""
    bridge._stream_sid = None
    await bridge._dispatch({"type": "response.output_audio.delta", "delta": mulaw_silence()})
    assert bridge.twilio.sent == []


async def test_empty_audio_delta_is_ignored(bridge):
    await bridge._dispatch({"type": "response.output_audio.delta", "delta": ""})
    assert bridge.twilio.sent == []


async def test_caller_audio_is_forwarded_to_xai(bridge):
    payload = mulaw_silence()
    bridge._twilio = _FakeTwilioWS(
        [
            {"event": "start", "start": {"streamSid": "MZabc"}},
            {"event": "media", "media": {"payload": payload}},
            {"event": "stop"},
        ]
    )
    await bridge._pump_twilio_to_xai()
    appends = [e for e in bridge.xai_sent if e["type"] == "input_audio_buffer.append"]
    assert appends == [{"type": "input_audio_buffer.append", "audio": payload}]
    assert bridge._stream_sid == "MZabc"


async def test_pump_returns_on_stop_event(bridge):
    bridge._twilio = _FakeTwilioWS([{"event": "stop"}])
    await bridge._pump_twilio_to_xai()  # must return, not hang


async def test_malformed_twilio_frame_does_not_kill_the_call(bridge):
    class _Bad(_FakeTwilioWS):
        async def receive_text(self):
            if not self._inbound:
                return json.dumps({"event": "stop"})
            item = self._inbound.pop(0)
            # Raw strings pass through unencoded so a non-JSON frame reaches
            # the pump exactly as Twilio would deliver it.
            return item if isinstance(item, str) else json.dumps(item)

    bridge._twilio = _Bad(["not json at all", {"event": "stop"}])
    await bridge._pump_twilio_to_xai()


# --------------------------------------------------------------------------- #
# Barge-in
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "etype",
    ["input_audio_buffer.speech_started", "input_audio_buffer.speech_start"],
)
async def test_caller_interruption_clears_buffered_playback(bridge, etype):
    """Twilio buffers audio ahead of playback. Without an explicit clear, an
    interrupted caller keeps hearing the rest of the agent's sentence."""
    await bridge._dispatch({"type": etype})
    assert bridge.twilio.sent == [{"event": "clear", "streamSid": "MZ1234567890"}]


async def test_clear_is_skipped_before_stream_sid_is_known(bridge):
    bridge._stream_sid = None
    await bridge._dispatch({"type": "input_audio_buffer.speech_started"})
    assert bridge.twilio.sent == []


# --------------------------------------------------------------------------- #
# Conversation handling is inherited, not reimplemented
# --------------------------------------------------------------------------- #
async def test_transcripts_still_accumulate_through_the_bridge(bridge):
    await bridge._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": "Do you have anything Wednesday?"},
        }
    )
    await bridge._dispatch(
        {
            "type": "response.output_audio_transcript.done",
            "data": {"transcript": "We do, at two o'clock."},
        }
    )
    assert bridge.session.transcript_text.splitlines() == [
        "Agent: Thank you for calling Healing Waters Day Spa.",
        "Caller: Do you have anything Wednesday?",
        "Agent: We do, at two o'clock.",
    ]


async def test_agent_transcript_deltas_are_accumulated_into_one_turn(bridge):
    """Observed live: the agent's words arrive as `.delta` events with no
    transcript `.done`. Treating each delta as a turn shreds the transcript;
    ignoring them loses the agent's half of the call entirely."""
    for delta in ("Thank you for calling Healing Waters Day", " Spa. How can I help you today?"):
        await bridge._dispatch(
            {"type": "response.output_audio_transcript.delta", "delta": delta}
        )
    # No turn yet — the response is still streaming.
    assert len(bridge.session.history) == 1
    await bridge._dispatch({"type": "response.output_audio.done"})

    assert bridge.session.history[-1] == {
        "role": "assistant",
        "content": "Thank you for calling Healing Waters Day Spa. How can I help you today?",
    }


async def test_response_done_flushes_a_pending_agent_turn(bridge):
    """Belt and braces: if no audio-done arrives, `response.done` still closes
    the turn so nothing is stranded in the buffer."""
    await bridge._dispatch(
        {"type": "response.output_audio_transcript.delta", "delta": "We open at ten."}
    )
    await bridge._dispatch({"type": "response.done"})
    assert bridge.session.history[-1]["content"] == "We open at ten."


async def test_audio_events_are_not_logged_as_transcript_turns(bridge):
    """Audio deltas arrive many times a second; treating one as a turn would
    flood the transcript."""
    for _ in range(5):
        await bridge._dispatch(
            {"type": "response.output_audio.delta", "delta": mulaw_silence()}
        )
    assert len(bridge.session.history) == 1


def test_mulaw_silence_is_valid_base64_frames():
    raw = base64.b64decode(mulaw_silence(3))
    assert len(raw) == 480  # 3 x 160-byte 20ms frames


# --------------------------------------------------------------------------- #
# Playback ordering
# --------------------------------------------------------------------------- #
async def test_concurrent_audio_deltas_play_in_order(bridge):
    """The agent's own voice must not be shuffled on the way to the caller.

    Each delta used to be played by its own asyncio.Task. Every frame send is
    an await, so two overlapping deltas interleaved their 20ms frames on the
    wire and the caller heard a garbled, stuttering voice. One queue and one
    writer make the ordering a property of the design rather than of timing.
    """
    bridge._playback_task = asyncio.create_task(bridge._playback_worker())
    try:
        # Two deltas, each several frames long and individually identifiable.
        first = bytes([0x01]) * (MULAW_FRAME_BYTES * 3)
        second = bytes([0x02]) * (MULAW_FRAME_BYTES * 3)
        await bridge._dispatch(
            {"type": "response.output_audio.delta", "delta": base64.b64encode(first).decode()}
        )
        await bridge._dispatch(
            {"type": "response.output_audio.delta", "delta": base64.b64encode(second).decode()}
        )
        await bridge._play_queue.join()
    finally:
        bridge._playback_task.cancel()
        bridge._playback_task = None

    payloads = [
        base64.b64decode(m["media"]["payload"])
        for m in bridge.twilio.sent
        if m.get("event") == "media"
    ]
    assert len(payloads) == 2
    # All of the first delta, then all of the second — never interleaved.
    assert payloads[0] == first
    assert payloads[1] == second


async def test_interruption_discards_queued_frames_not_just_twilios_buffer(bridge):
    """Clearing Twilio's buffer is not enough: anything still queued on our
    side would be sent straight after the clear and resume the interrupted
    sentence."""
    bridge._enqueue_audio(base64.b64encode(bytes([0x01]) * MULAW_FRAME_BYTES).decode())
    assert not bridge._play_queue.empty()

    await bridge._stop_playback()

    assert bridge._play_queue.empty()
    assert bridge.twilio.sent[-1] == {"event": "clear", "streamSid": "MZ1234567890"}


async def test_interruption_sends_clear_through_the_single_playback_worker(bridge):
    """The worker must serialize clear with media on the Twilio socket."""
    # A response must be active for the barge-in cancel to actually fire —
    # as it would be in a real session, since the caller is interrupting it.
    bridge._active_response_id = "resp-1"
    bridge._playback_task = asyncio.create_task(bridge._playback_worker())
    try:
        await bridge._dispatch({"type": "input_audio_buffer.speech_started"})
        await bridge._play_queue.join()
    finally:
        bridge._playback_task.cancel()
        bridge._playback_task = None

    assert bridge.twilio.sent == [{"event": "clear", "streamSid": "MZ1234567890"}]


async def test_barge_in_clears_over_a_second_of_queued_audio_and_drops_late_stragglers(bridge):
    """Full barge-in trace: >1s already queued for the response being
    interrupted must be dropped, AND a straggler delta that still arrives
    tagged with that now-cancelled response id (already in flight over the
    socket when we sent response.cancel) must also never reach Twilio.
    Only audio belonging to the caller's new turn may play afterward."""
    bridge._active_response_id = "resp-old"
    bridge._playback_task = asyncio.create_task(bridge._playback_worker())
    try:
        # 60 frames * 20ms = 1.2s queued for the response about to be cut off.
        await bridge._dispatch(
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-old",
                "delta": mulaw_silence(60),
            }
        )
        assert bridge._play_queue.qsize() > 0

        await bridge._dispatch({"type": "input_audio_buffer.speech_started"})
        await bridge._play_queue.join()

        # A straggler for the SAME (now cancelled) response, arriving after
        # the barge-in was already processed.
        await bridge._dispatch(
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-old",
                "delta": base64.b64encode(bytes([0x02]) * MULAW_FRAME_BYTES).decode(),
            }
        )
        assert bridge._play_queue.qsize() == 0, "a straggler from the cancelled response must not be queued"

        # The caller's new turn gets its own response; its audio must play.
        await bridge._dispatch({"type": "response.created", "response": {"id": "resp-new"}})
        await bridge._dispatch(
            {
                "type": "response.output_audio.delta",
                "response_id": "resp-new",
                "delta": base64.b64encode(bytes([0x03]) * MULAW_FRAME_BYTES).decode(),
            }
        )
        await bridge._play_queue.join()
    finally:
        bridge._playback_task.cancel()
        bridge._playback_task = None

    media_payloads = [
        base64.b64decode(m["media"]["payload"])
        for m in bridge.twilio.sent
        if m.get("event") == "media"
    ]
    assert media_payloads == [bytes([0x03]) * MULAW_FRAME_BYTES], (
        "only the new response's audio may have reached Twilio"
    )
    assert bridge.twilio.sent[0] == {"event": "clear", "streamSid": "MZ1234567890"}
    assert bridge.xai_sent == [{"type": "response.cancel"}]


async def test_confirmation_audio_is_not_dropped_after_stale_clear(bridge):
    """Clearing the tool turn must not mute the booking confirmation."""
    bridge._pending_forced_tool_message = "You're all set."
    bridge._playback_task = asyncio.create_task(bridge._playback_worker())
    try:
        await bridge._dispatch(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(bytes([0x01]) * 320).decode(),
            }
        )
        await bridge._dispatch({"type": "response.done"})
        assert bridge._allow_audio is True
        confirmation = bytes([0x07]) * 640
        await bridge._dispatch(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(confirmation).decode(),
            }
        )
        await bridge._play_queue.join()
    finally:
        bridge._playback_task.cancel()
        bridge._playback_task = None

    media = [
        base64.b64decode(m["media"]["payload"])
        for m in bridge.twilio.sent
        if m.get("event") == "media"
    ]
    assert confirmation in media


async def test_confirmation_playback_is_not_cleared_by_barge_in(bridge):
    bridge._protect_playback = True
    bridge._playback_task = asyncio.create_task(bridge._playback_worker())
    try:
        await bridge._dispatch(
            {
                "type": "response.output_audio.delta",
                "delta": base64.b64encode(bytes([0x09]) * 160).decode(),
            }
        )
        await bridge._dispatch({"type": "input_audio_buffer.speech_started"})
        await bridge._play_queue.join()
    finally:
        bridge._playback_task.cancel()
        bridge._playback_task = None
    assert {"event": "clear", "streamSid": "MZ1234567890"} not in bridge.twilio.sent
    assert bridge.xai_sent == []


async def test_hold_tone_stops_immediately_and_does_not_overlap_reply(bridge):
    await bridge.start_hold_tone()
    assert bridge._hold_active is True
    assert bridge._play_queue.qsize() <= 1
    await bridge.start_hold_tone()
    assert bridge._hold_task is not None
    await asyncio.sleep(0.05)
    queued_before = bridge._queued_audio_bytes
    await bridge.stop_hold_tone()
    assert bridge._hold_active is False
    assert bridge._play_queue.empty() or bridge._queued_audio_bytes <= queued_before
    reply = bytes([0x04]) * 200
    await bridge._dispatch(
        {"type": "response.output_audio.delta", "delta": base64.b64encode(reply).decode()}
    )
    media = [
        base64.b64decode(m["media"]["payload"])
        for m in bridge.twilio.sent
        if m.get("event") == "media"
    ]
    assert reply in media


async def test_barge_in_clears_hold_audio(bridge):
    bridge.session.greeting_sent = True
    await bridge.start_hold_tone()
    await asyncio.sleep(0.05)
    await bridge._dispatch({"type": "input_audio_buffer.speech_started"})
    assert bridge._hold_active is False
    assert any(m.get("event") == "clear" for m in bridge.twilio.sent)
