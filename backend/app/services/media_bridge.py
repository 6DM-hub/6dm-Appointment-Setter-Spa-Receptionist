"""
Bridge a Twilio Media Stream to an xAI realtime speech-to-speech session.

Twilio's <Connect><Stream> opens a WebSocket to us and pushes the caller's audio
as base64 frames; anything we send back is played to the caller. xAI's realtime
socket takes the same shape of payload. Both sides speak G.711 μ-law at 8 kHz —
Twilio's `PCMU` codec and xAI's `audio/pcmu` format — so this relays frames
verbatim in both directions with no resampling or transcoding.

Why this exists rather than the SIP route in app/services/xai_realtime.py:
pointing the number's SIP trunk at sip.voice.x.ai hands the whole call to a
console-configured agent that cannot see the database, so every spa gets the
same receptionist and nothing is ever transcribed. Registering the number with
xAI to get webhooks instead requires an entitlement this team does not have.
Bridging the audio ourselves needs neither — a plain API key opens the socket —
and keeps per-tenant prompts, the booking engine and transcripts intact.

The conversation half (session config, tools, transcript accumulation) is reused
from XAIVoiceSession; this module adds only the audio plumbing and the Twilio
protocol.
"""
import asyncio
import base64
import binascii
import json
import logging
from typing import Any

from fastapi import WebSocketDisconnect

from app.core.config import settings
from app.services.call_state import CallSession
from app.services.truth_log import truth
from app.services.voice_config import resolve_xai_voice
from app.services.xai_realtime import (
    XAI_REALTIME_MODEL,
    XAIVoiceSession,
    build_xai_realtime_url,
    _AUDIO_DELTA_EVENTS,
    _FUNCTION_CALL_DONE,
)

logger = logging.getLogger(__name__)

# G.711 μ-law is 8-bit at 8 kHz, so 8,000 decoded bytes represent ~1 second
# of audio. Twilio Media Streams accepts variable-sized μ-law payloads and
# buffers media messages in order; keep each xAI delta as one ordered queue
# item instead of exploding it into dozens of local WebSocket sends.
MULAW_FRAME_BYTES = 160  # retained for silence/test helpers (20 ms of μ-law)
MULAW_BYTES_PER_SECOND = 8000
_MULAW_SILENCE_BYTE = 0xFF  # PCMU zero-amplitude sample


def mulaw_silence(frames: int = 1) -> str:
    """Base64 mu-law silence, `frames` * 20ms long. Test/no-op-audio helper."""
    return base64.b64encode(bytes([_MULAW_SILENCE_BYTE]) * (MULAW_FRAME_BYTES * frames)).decode()

TWILIO_EVENT_START = "start"
TWILIO_EVENT_MEDIA = "media"
TWILIO_EVENT_STOP = "stop"

# xAI audio events. The realtime API is OpenAI-Realtime-compatible, so both the
# current and legacy spellings are accepted rather than betting on one.
_SPEECH_STARTED_EVENTS = (
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_start",
)


class TwilioMediaBridge(XAIVoiceSession):
    """One phone call: Twilio audio in, xAI audio out.

    Inherits the conversation handling (per-spa `session.update`, greeting,
    `manage_appointment` tool calls, transcript persistence) and overrides only
    what differs: audio format negotiation, and pumping frames between the two
    sockets.
    """

    def __init__(self, call_sid: str, session: CallSession, twilio_ws: Any) -> None:
        super().__init__(call_sid, session)
        self._twilio = twilio_ws
        self._stream_sid: str | None = None
        # Single-consumer playback queue. See `_playback_worker` for why the
        # previous task-per-delta approach corrupted the agent's voice.
        self._play_queue: asyncio.Queue[tuple[str, bytes, int]] = asyncio.Queue()
        self._queued_audio_bytes = 0
        self._playback_task: asyncio.Task | None = None
        self._play_generation = 0
        self._allow_audio = True
        self._first_twilio_audio_logged = False
        self._protect_playback = False
        self._confirmation_response_open = False
        self._hold_active = False
        self._hold_task: asyncio.Task | None = None

    def _outbound_media_ready(self) -> bool:
        return bool(self._stream_sid) and self._playback_task is not None

    async def _await_xai_session_ready(self, timeout: float = 5.0) -> None:
        if self._xai_session_ready.is_set():
            return
        try:
            await asyncio.wait_for(self._xai_session_ready.wait(), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "call %s: xAI session.created not seen; greeting after Twilio ready",
                self.call_id,
            )

    async def _maybe_greet_if_outbound_ready(self) -> None:
        if not self._outbound_media_ready():
            return
        if self.session.greeting_sent:
            return
        await self._greet()

    # ------------------------------------------------------------------ setup
    @property
    def _call_log_sid(self) -> str:
        # `voice_inbound` already created the row under Twilio's own CallSid, so
        # the `xai:` prefix used by the SIP path would find nothing here.
        return self.call_id

    @property
    def _url(self) -> str:
        # No call_id: this is a plain realtime session, not an xAI-hosted SIP
        # call, so nothing needs to be registered with xAI beforehand.
        return build_xai_realtime_url()

    async def _configure(self) -> None:
        """Same persona as the SIP path, plus μ-law so Twilio frames pass through."""
        from app.services.grok_service import build_realtime_instructions

        instructions = build_realtime_instructions(
            self.session.business_name or settings.APP_NAME,
            self.session.tenant_prompt,
            self.session.timezone,
            direction=self.session.direction,
            call_objective=self.session.call_objective,
        )
        requested_voice = resolve_xai_voice(
            self.session.entities.get("xai_voice") or settings.XAI_VOICE_ID
        )
        logger.info(
            "Creating xAI realtime session call=%s voice=%s realtime_model=%s text_model=%s",
            self.call_id,
            requested_voice,
            XAI_REALTIME_MODEL,
            settings.GROK_MODEL,
        )
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "instructions": instructions,
                    "voice": requested_voice,
                    "turn_detection": {"type": "server_vad"},
                    "tools": self._booking_tools(),
                    # G.711 μ-law 8kHz both ways — byte-identical to Twilio's
                    # PCMU payloads, so no transcoding is needed anywhere.
                    "audio": {
                        "input": {"format": {"type": "audio/pcmu"}},
                        "output": {
                            "format": {"type": "audio/pcmu"},
                            # Explicit normal speed keeps speech pacing stable.
                            "speed": 1.0,
                        },
                    },
                },
            }
        )

    def _booking_tools(self) -> list[dict[str, Any]]:
        """Same propose/confirm split as the SIP path — one source of truth."""
        from app.services.xai_realtime import VOICE_TOOLS

        if self.session.direction == "outbound":
            return [tool for tool in VOICE_TOOLS if tool["name"] in {
                "check_availability", "propose_appointment", "confirm_appointment",
            }]
        return VOICE_TOOLS

    # ------------------------------------------------------------ Twilio side
    async def _to_twilio(self, payload: dict[str, Any]) -> None:
        await self._twilio.send_text(json.dumps(payload))

    def _enqueue_audio(self, audio_b64: str) -> bool:
        """Queue one complete xAI μ-law audio delta in generation order.

        Keeping one queue item per xAI delta avoids turning a single server
        event into dozens of local WebSocket sends. Byte accounting, rather
        than queue length, is used to estimate the actual audio backlog.
        """
        try:
            raw = base64.b64decode(audio_b64)
        except (ValueError, binascii.Error):
            logger.warning("call %s: undecodable audio delta dropped", self.call_id)
            return False
        if not self._allow_audio or not raw:
            return False

        self._play_queue.put_nowait(("audio", raw, self._play_generation))
        self._queued_audio_bytes += len(raw)

        queued_ms = (self._queued_audio_bytes / MULAW_BYTES_PER_SECOND) * 1000
        if queued_ms > 3000:
            logger.warning(
                "call %s: AUDIO_BACKLOG queued_bytes=%d queued_ms=%.0f items=%d",
                self.call_id,
                self._queued_audio_bytes,
                queued_ms,
                self._play_queue.qsize(),
            )
        return True

    async def _playback_worker(self) -> None:
        """The sole writer of agent audio to Twilio."""
        while True:
            kind, raw, generation = await self._play_queue.get()
            try:
                if not self._stream_sid:
                    if kind == "audio":
                        self._play_queue.put_nowait((kind, raw, generation))
                        await asyncio.sleep(0.02)
                    continue
                if kind == "clear":
                    await self._to_twilio(
                        {"event": "clear", "streamSid": self._stream_sid}
                    )
                    continue
                if generation != self._play_generation or not self._allow_audio:
                    continue
                await self._to_twilio(
                    {
                        "event": "media",
                        "streamSid": self._stream_sid,
                        "media": {"payload": base64.b64encode(raw).decode()},
                    }
                )
                if not self._first_twilio_audio_logged:
                    self._first_twilio_audio_logged = True
                    logger.info("call %s: first Twilio audio chunk sent", self.call_id)
            except Exception:
                logger.exception("call %s: playback media send failed", self.call_id)
            finally:
                if kind == "audio":
                    self._queued_audio_bytes = max(0, self._queued_audio_bytes - len(raw))
                self._play_queue.task_done()

    async def _play(self, audio_b64: str) -> None:
        """Queue a delta and let the worker drain it (kept for callers/tests)."""
        self._enqueue_audio(audio_b64)
        if self._playback_task is None:
            # No worker running (unit tests drive `_play` directly): flush
            # synchronously so behaviour matches the live path.
            await self._drain_playback_queue()

    async def _drain_playback_queue(self) -> None:
        while not self._play_queue.empty():
            kind, raw, generation = self._play_queue.get_nowait()
            try:
                if kind == "clear":
                    await self._to_twilio({"event": "clear", "streamSid": self._stream_sid})
                    continue
                if (
                    self._stream_sid
                    and generation == self._play_generation
                    and self._allow_audio
                ):
                    await self._to_twilio(
                        {
                            "event": "media",
                            "streamSid": self._stream_sid,
                            "media": {"payload": base64.b64encode(raw).decode()},
                        }
                    )
            finally:
                if kind == "audio":
                    self._queued_audio_bytes = max(0, self._queued_audio_bytes - len(raw))
                self._play_queue.task_done()

    async def _stop_playback(self) -> None:
        """Drop audio Twilio has buffered but not yet played.

        Without this a caller who interrupts keeps hearing the rest of the
        agent's previous sentence, because Twilio has already buffered it —
        the barge-in feels broken even though the model stopped talking.
        """
        self._play_generation += 1
        self._allow_audio = False
        cleared_bytes = 0
        while not self._play_queue.empty():
            kind, raw, _generation = self._play_queue.get_nowait()
            if kind == "audio":
                cleared_bytes += len(raw)
            self._play_queue.task_done()
        self._queued_audio_bytes = max(0, self._queued_audio_bytes - cleared_bytes)
        if cleared_bytes:
            logger.info(
                "call %s: BARGE_IN queue_cleared bytes=%d (~%.0fms)",
                self.call_id, cleared_bytes,
                (cleared_bytes / MULAW_BYTES_PER_SECOND) * 1000,
            )
        if not self._stream_sid:
            return
        if self._playback_task is None:
            await self._to_twilio({"event": "clear", "streamSid": self._stream_sid})
        else:
            self._play_queue.put_nowait(("clear", b"", self._play_generation))

    async def _drop_stale_audio_for_confirmation(self) -> None:
        """Drop the tool-turn audio without muting the confirmation that follows.

        `_stop_playback` sets `_allow_audio` false until the next
        `response.created`. Confirmation frames that arrive in that gap were
        discarded, so the caller heard a cut-off or missing booking line.
        """
        self._play_generation += 1
        cleared_bytes = 0
        while not self._play_queue.empty():
            kind, raw, _generation = self._play_queue.get_nowait()
            if kind == "audio":
                cleared_bytes += len(raw)
            self._play_queue.task_done()
        self._queued_audio_bytes = max(0, self._queued_audio_bytes - cleared_bytes)
        self._allow_audio = True
        if self._stream_sid:
            if self._playback_task is None:
                await self._to_twilio({"event": "clear", "streamSid": self._stream_sid})
            else:
                self._play_queue.put_nowait(("clear", b"", self._play_generation))

    async def start_hold_tone(self) -> None:
        """One quiet looping tone while a provider call is still running."""
        if self._hold_active or getattr(self, "_protect_playback", False):
            return
        self._hold_active = True

        async def _loop() -> None:
            from app.services.hold_tone import soft_hold_chunk

            chunk = soft_hold_chunk()
            while self._hold_active:
                if self._queued_audio_bytes > MULAW_BYTES_PER_SECOND:
                    await asyncio.sleep(0.2)
                    continue
                self._play_generation_hold = self._play_generation
                self._play_queue.put_nowait(("audio", chunk, self._play_generation))
                self._queued_audio_bytes += len(chunk)
                await asyncio.sleep(2.0)

        self._hold_task = asyncio.create_task(_loop())

    async def stop_hold_tone(self) -> None:
        """Stop the waiting tone immediately. Does not mute a later reply."""
        was_active = self._hold_active
        self._hold_active = False
        task = self._hold_task
        self._hold_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if was_active:
            await self._drop_stale_audio_for_confirmation()

    async def _cancel_active_response(self) -> None:
        """Also drop Twilio-queued audio. A confirmation-guard cancel that
        only hits xAI still lets already-buffered μ-law frames play, which
        sounds like crackle and a cut-off sentence."""
        await super()._cancel_active_response()
        await self._stop_playback()

    # --------------------------------------------------------------- xAI side
    async def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")

        if etype in _AUDIO_DELTA_EVENTS:
            rid = self._response_id_of(event)
            if rid and rid in self._cancelled_response_ids:
                return
            if rid and rid == getattr(self, "_muted_availability_response_id", None):
                return
            delta = event.get("delta") or event.get("audio")
            if isinstance(delta, str) and delta:
                self._mark_timing("TTS FIRST AUDIO")
                if not self._first_xai_audio_logged:
                    self._first_xai_audio_logged = True
                    logger.info("call %s: first xAI audio received", self.call_id)
                accepted = self._enqueue_audio(delta)
                if accepted and self._stream_sid:
                    await self._note_greeting_audio_started(rid)
                if self._playback_task is None:
                    await self._drain_playback_queue()
            return

        if etype in _SPEECH_STARTED_EVENTS:
            self._start_turn()
            if getattr(self, "_protect_playback", False):
                logger.info(
                    "call %s: caller speech during booking confirmation; keeping that audio",
                    self.call_id,
                )
                return
            await self.stop_hold_tone()
            self._pending_availability_speech = None
            self._availability_speech_interrupted = True
            if self.session.greeting_sent:
                await self._cancel_active_response()
            elif self._greeting_pending:
                logger.info(
                    "call %s: caller speech during opening greeting; keeping the one greeting",
                    self.call_id,
                )
            else:
                await self._cancel_active_response()
            return

        if etype == "response.created":
            self._allow_audio = True
            if getattr(self, "_protect_playback", False):
                self._confirmation_response_open = True

        # A successful booking confirmation is emitted by the parent as a
        # deterministic force_message immediately after the tool-calling
        # response closes. response.done means xAI finished GENERATING that old
        # response; it does not prove Twilio finished PLAYING audio already sent.
        #
        # Drop that older audio, but keep accepting the confirmation frames.
        if (
            etype == "response.done"
            and getattr(self, "_pending_forced_tool_message", None) is not None
            and not getattr(self, "_confirmation_response_open", False)
        ):
            logger.info(
                "call %s: clearing stale Twilio playback before authoritative force_message",
                self.call_id,
            )
            await self._drop_stale_audio_for_confirmation()
        confirmation_finished = (
            etype == "response.done" and getattr(self, "_confirmation_response_open", False)
        )
        if confirmation_finished:
            self._confirmation_response_open = False
            self._protect_playback = False

        # Everything else — transcripts, tool calls, errors — is conversation
        # handling, which the parent already implements.
        await super()._dispatch(event)
        if confirmation_finished and getattr(self, "_deferred_wrap_up", False):
            self._deferred_wrap_up = False
            self._awaiting_wrap_up = False
            await self._send_force_message(
                "Perfect. We look forward to seeing you. Have a great day!"
            )

    # ------------------------------------------------------------------ pumps
    async def _pump_twilio_to_xai(self) -> None:
        """Forward caller audio until Twilio hangs up."""
        while True:
            raw = await self._twilio.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue

            event = message.get("event")
            if event == TWILIO_EVENT_MEDIA:
                payload = (message.get("media") or {}).get("payload")
                if payload:
                    if not self._first_caller_audio_logged:
                        self._first_caller_audio_logged = True
                        logger.info("call %s: first caller audio received", self.call_id)
                    await self._send(
                        {"type": "input_audio_buffer.append", "audio": payload}
                    )
            elif event == TWILIO_EVENT_START:
                start = message.get("start") or {}
                self._stream_sid = start.get("streamSid")
                logger.info(
                    "call %s: Twilio stream %s started", self.call_id, self._stream_sid
                )
                if self._playback_task is not None:
                    truth(
                        "TWILIO_OUTBOUND_READY",
                        call_sid=self.call_id,
                        stream_sid=self._stream_sid,
                    )
                    await self._maybe_greet_if_outbound_ready()
            elif event == TWILIO_EVENT_STOP:
                logger.info("call %s: Twilio stream stopped", self.call_id)
                return

    async def _pump_xai_to_twilio(self) -> None:
        """Forward agent audio and conversation events until the socket closes."""
        import websockets

        while True:
            try:
                raw = await self._ws.recv()
            except websockets.exceptions.ConnectionClosed:
                return
            try:
                    event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            # A Square lookup must not stall this pump, or the holding phrase
            # cannot play until the lookup returns.
            etype = event.get("type") if isinstance(event, dict) else None
            if etype in _FUNCTION_CALL_DONE:
                task = asyncio.create_task(self._dispatch(event))
                self._inflight_tools.add(task)
                task.add_done_callback(self._inflight_tools.discard)
                continue
            await self._dispatch(event)

    async def run(self) -> None:  # type: ignore[override]
        """Hold the call open until either side ends it, then persist."""
        import websockets
        from websockets.asyncio.client import connect as ws_connect

        headers = {"Authorization": f"Bearer {settings.XAI_API_KEY}"}
        try:
            async with ws_connect(self._url, additional_headers=headers) as ws:
                self._ws = ws
                await self._configure()
                logger.info("call %s: xAI realtime bridge open", self.call_id)

                xai_pump = asyncio.create_task(self._pump_xai_to_twilio())
                await self._await_xai_session_ready()
                await self._await_stream_start()
                self._playback_task = asyncio.create_task(self._playback_worker())
                if self._stream_sid:
                    truth(
                        "TWILIO_OUTBOUND_READY",
                        call_sid=self.call_id,
                        stream_sid=self._stream_sid,
                    )
                    await self._greet()

                done, pending = await asyncio.wait(
                    [
                        asyncio.create_task(self._pump_twilio_to_xai()),
                        xai_pump,
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=settings.XAI_VOICE_MAX_CALL_SECONDS,
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    exc = task.exception()
                    # A caller hanging up closes the Twilio socket; that is the
                    # normal end of every call, not a failure worth an ERROR.
                    if exc and not isinstance(
                        exc,
                        (
                            websockets.exceptions.ConnectionClosed,
                            WebSocketDisconnect,
                        ),
                    ):
                        logger.error("call %s: bridge pump failed: %r", self.call_id, exc)
        except Exception:
            logger.exception("call %s: realtime bridge failed", self.call_id)
        finally:
            await self.stop_hold_tone()
            if self._playback_task is not None:
                self._playback_task.cancel()
                self._playback_task = None
            self._flush_caller_turn()
            await self._finalize()

    async def _await_stream_start(self, timeout: float = 10.0) -> None:
        """Consume Twilio frames until the `start` event supplies the streamSid."""
        deadline = asyncio.get_running_loop().time() + timeout
        while self._stream_sid is None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                logger.warning(
                    "call %s: no Twilio start event yet; waiting in media pump",
                    self.call_id,
                )
                return
            try:
                raw = await asyncio.wait_for(self._twilio.receive_text(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning("call %s: timed out waiting for Twilio start", self.call_id)
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if message.get("event") == TWILIO_EVENT_START:
                self._stream_sid = (message.get("start") or {}).get("streamSid")
                logger.info(
                    "call %s: Twilio stream %s started", self.call_id, self._stream_sid
                )
            elif message.get("event") == TWILIO_EVENT_MEDIA:
                # Audio can precede our readiness; forward it rather than clip
                # the first word the caller says.
                payload = (message.get("media") or {}).get("payload")
                if payload:
                    await self._send(
                        {"type": "input_audio_buffer.append", "audio": payload}
                    )
