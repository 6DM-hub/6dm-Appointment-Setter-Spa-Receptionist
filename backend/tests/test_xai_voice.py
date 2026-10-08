"""Tests for the xAI Voice Agent inbound path.

The realtime WebSocket itself cannot be exercised here — it needs Voice Agent
entitlement on the xAI team — so these cover everything decided *before* and
*after* the socket: webhook authentication, SIP identification, transcript
accumulation, and the booking tool bridge. Those are the parts where a mistake
would silently lose a call or a transcript.
"""
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api.deps import verify_xai_voice_signature
from app.api.v1.telephony import _authoritative_reply
from app.api.v1.xai_voice import _sip_header
from app.services.phone_numbers import normalize_phone_target
from app.core.config import settings
from app.services.call_state import CallSession
from app.services.grok_service import build_realtime_instructions
from app.services.grok_service import primary_caller_language
from app.services.xai_realtime import (
    CHECK_AVAILABILITY_TOOL,
    MANAGE_APPOINTMENT_TOOL,
    XAIVoiceSession,
    _first_str,
    xai_call_sid,
)
from app.services.appointment_booking_service import BookingOutcome, BookingResult, apply_booking_result

SECRET = "whsec_" + base64.b64encode(b"super-secret-signing-key").decode()


# --------------------------------------------------------------------------- #
# SIP identification
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("sip:+18058878345@sip.voice.x.ai;transport=tls", "+18058878345"),
        ("sips:+18058878345@sip.voice.x.ai", "+18058878345"),
        ("<+18058878345>", "+18058878345"),
        ("+18058878345", "+18058878345"),
        ("  +18058878345  ", "+18058878345"),
        (None, None),
        ("", None),
    ],
)
def test_normalize_phone_target_reduces_sip_uri_to_e164(raw, expected):
    """The dialed number is what resolves the tenant, so every SIP form the
    trunk might present has to reduce to the stored E.164 value."""
    assert normalize_phone_target(raw) == expected


def test_sip_header_is_case_insensitive():
    headers = [{"name": "FROM", "value": "+14155550100"}, {"name": "to", "value": "+18005550199"}]
    assert _sip_header(headers, "From") == "+14155550100"
    assert _sip_header(headers, "To") == "+18005550199"
    assert _sip_header(headers, "Diversion") is None


def test_sip_header_tolerates_malformed_entries():
    assert _sip_header([None, "junk", {"noname": 1}], "From") is None


def test_xai_call_sid_fits_the_call_log_column():
    """`call_logs.twilio_call_sid` is String(64); a prefixed UUID must fit or
    every xAI call would fail to insert."""
    sid = xai_call_sid(str(uuid.uuid4()))
    assert sid.startswith("xai:")
    assert len(sid) <= 64


# --------------------------------------------------------------------------- #
# Webhook authentication
# --------------------------------------------------------------------------- #
class _FakeRequest:
    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


def _sign(body: bytes, webhook_id: str, timestamp: str, secret: str = SECRET) -> str:
    key = base64.b64decode(secret[len("whsec_") :])
    signed = b".".join([webhook_id.encode(), timestamp.encode(), body])
    digest = hmac.new(key, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def _request(body: bytes, *, signature: str | None = None, timestamp: str | None = None):
    webhook_id = "msg_123"
    ts = timestamp or str(int(time.time()))
    sig = signature if signature is not None else _sign(body, webhook_id, ts)
    return _FakeRequest(
        body,
        {"webhook-id": webhook_id, "webhook-timestamp": ts, "webhook-signature": sig},
    )


@pytest.fixture
def signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "XAI_VOICE_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(settings, "APP_ENV", "development")
    return SECRET


async def test_valid_signature_is_accepted(signing_secret):
    body = json.dumps({"type": "realtime.call.incoming"}).encode()
    await verify_xai_voice_signature(_request(body))


async def test_tampered_body_is_rejected(signing_secret):
    body = json.dumps({"type": "realtime.call.incoming"}).encode()
    request = _request(body)
    request._body = body + b" "  # signature no longer covers the payload
    with pytest.raises(HTTPException) as exc:
        await verify_xai_voice_signature(request)
    assert exc.value.status_code == 403


async def test_wrong_secret_is_rejected(signing_secret):
    body = b"{}"
    other = "whsec_" + base64.b64encode(b"a-different-key").decode()
    with pytest.raises(HTTPException) as exc:
        await verify_xai_voice_signature(
            _request(body, signature=_sign(body, "msg_123", str(int(time.time())), other))
        )
    assert exc.value.status_code == 403


async def test_stale_timestamp_is_rejected(signing_secret):
    """Replay protection: a captured delivery must not be replayable later."""
    body = b"{}"
    old = str(int(time.time()) - settings.XAI_WEBHOOK_TOLERANCE_SECONDS - 60)
    with pytest.raises(HTTPException) as exc:
        await verify_xai_voice_signature(_request(body, timestamp=old))
    assert exc.value.status_code == 403


async def test_missing_headers_are_rejected(signing_secret):
    with pytest.raises(HTTPException) as exc:
        await verify_xai_voice_signature(_FakeRequest(b"{}", {}))
    assert exc.value.status_code == 403


async def test_malformed_timestamp_is_rejected(signing_secret):
    with pytest.raises(HTTPException) as exc:
        await verify_xai_voice_signature(_request(b"{}", timestamp="not-a-number"))
    assert exc.value.status_code == 403


async def test_signature_accepted_during_secret_rotation(signing_secret):
    """xAI sends several `v1,<sig>` pairs while a secret is rotating; matching
    any one of them is a valid delivery."""
    body = b"{}"
    ts = str(int(time.time()))
    good = _sign(body, "msg_123", ts)
    stale = _sign(body, "msg_123", ts, "whsec_" + base64.b64encode(b"old-key").decode())
    await verify_xai_voice_signature(_request(body, signature=f"{stale} {good}", timestamp=ts))


async def test_production_refuses_to_run_unsigned(monkeypatch):
    """An unsigned webhook would let anyone start a billable voice session."""
    monkeypatch.setattr(settings, "XAI_VOICE_WEBHOOK_SECRET", "")
    monkeypatch.setattr(settings, "APP_ENV", "production")
    with pytest.raises(HTTPException) as exc:
        await verify_xai_voice_signature(_FakeRequest(b"{}", {}))
    assert exc.value.status_code == 500


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #
def test_realtime_instructions_carry_tenant_block_and_tool_contract():
    text = build_realtime_instructions("Healing Waters Day Spa", "SERVICE MENU:\n- Facial 60 minutes")
    assert "Healing Waters Day Spa" in text
    assert "SERVICE MENU:" in text
    # The two-step contract has to be spelled out, or the model books on
    # hearing a date and every change of mind becomes another appointment.
    assert "propose_appointment" in text
    assert "confirm_appointment" in text
    # Without this the model would announce bookings it never made.
    assert "booked=true" in text
    # A change of mind must be understood as an edit, not a second booking.
    assert "REPLACES the previous pending request" in text
    assert "Greet the caller only at the beginning of the call" in text


def test_realtime_instructions_survive_missing_tenant_prompt():
    text = build_realtime_instructions("6DM", None)
    assert "6DM" in text
    assert "{tenant_block}" not in text


# --------------------------------------------------------------------------- #
# Transcript accumulation
# --------------------------------------------------------------------------- #
def _session() -> CallSession:
    session = CallSession(
        call_sid="call-1",
        direction="inbound",
        from_number="+14155550100",
        to_number="+18058878345",
        business_name="Healing Waters Day Spa",
        tenant_id=str(uuid.uuid4()),
    )
    session.add_turn("assistant", "Thank you for calling Healing Waters Day Spa.")
    return session


@pytest.fixture
def voice_session(monkeypatch):
    voice = XAIVoiceSession("call-1", _session())

    async def _noop() -> None:
        return None

    monkeypatch.setattr(voice, "_persist_session", _noop)
    return voice


async def test_cumulative_caller_transcripts_yield_one_turn(voice_session):
    """The transcription events are cumulative. Appending each partial would
    write 'I' / 'I would' / 'I would like...' as three separate caller turns."""
    for partial in ("I", "I would", "I would like a facial"):
        await voice_session._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.updated",
                "data": {"transcript": partial},
            }
        )
    await voice_session._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": "I would like a facial"},
        }
    )
    user_turns = [t for t in voice_session.session.history if t["role"] == "user"]
    assert user_turns == [{"role": "user", "content": "I would like a facial"}]


async def test_agent_reply_closes_an_unflushed_caller_turn(voice_session):
    """If no `.completed` arrives, the agent starting to speak still marks the
    turn boundary — otherwise the caller's words never reach the transcript."""
    await voice_session._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.updated",
            "data": {"transcript": "do you have anything tomorrow"},
        }
    )
    await voice_session._dispatch(
        {
            "type": "response.output_audio_transcript.done",
            "data": {"transcript": "We do, at two o'clock."},
        }
    )
    roles = [t["role"] for t in voice_session.session.history]
    contents = [t["content"] for t in voice_session.session.history]
    assert roles == ["assistant", "user", "assistant"]
    assert contents[1] == "do you have anything tomorrow"
    assert contents[2] == "We do, at two o'clock."


async def test_transcript_text_renders_both_speakers(voice_session):
    await voice_session._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": "Hi there"},
        }
    )
    transcript = voice_session.session.transcript_text
    assert "Caller: Hi there" in transcript
    assert "Agent: Thank you for calling Healing Waters Day Spa." in transcript


async def test_repeated_completed_event_does_not_duplicate_a_turn(voice_session):
    for _ in range(2):
        await voice_session._dispatch(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "data": {"transcript": "Hello"},
            }
        )
    assert [t["content"] for t in voice_session.session.history].count("Hello") == 1


async def test_unknown_events_are_ignored_without_raising(voice_session):
    await voice_session._dispatch({"type": "some.future.event", "data": {"x": 1}})
    await voice_session._dispatch({})
    assert len(voice_session.session.history) == 1


def test_first_str_finds_nested_values():
    assert _first_str({"a": {"b": {"transcript": "found"}}}, "transcript") == "found"
    assert _first_str({"transcript": "  "}, "transcript") is None
    assert _first_str("not-a-dict", "transcript") is None


# --------------------------------------------------------------------------- #
# Booking tool bridge
# --------------------------------------------------------------------------- #
class _FakeSessionCtx:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def booking_stub(monkeypatch):
    """Swap the booking engine for a recorder; the engine itself is covered by
    tests/test_booking_adapters.py."""
    from app.services import xai_realtime

    calls: list = []

    class _Result:
        def __init__(self):
            from app.services.appointment_booking_service import BookingOutcome

            self.outcome = BookingOutcome.BOOKED
            self.appointment = None
            self.message = "Appointment successfully booked. Confirm it to the caller."

        def to_system_message(self) -> str:
            return f"[SYSTEM: {self.message}]"

    async def _fake_attempt(db, session, intent):
        calls.append(intent)
        return _Result()

    monkeypatch.setattr(xai_realtime, "AsyncSessionLocal", _FakeSessionCtx)
    monkeypatch.setattr(xai_realtime, "attempt_booking", _fake_attempt)
    return calls


async def test_tool_call_reaches_the_booking_engine(voice_session, booking_stub):
    output = await voice_session._run_manage_appointment(
        json.dumps(
            {
                "intent": "schedule",
                "caller_name": "Dana Reed",
                "requested_start_iso": "2026-09-04T14:00:00Z",
                "service_description": "60 minute deep tissue massage",
            }
        )
    )
    payload = json.loads(output)
    assert payload["booked"] is False
    assert payload["status"] == "rejected"
    assert booking_stub == []


async def test_availability_tool_does_not_call_booking_engine(voice_session, monkeypatch):
    from app.services import xai_realtime

    checked = []

    async def check(_db, session, intent):
        checked.append((session, intent))
        from app.services.appointment_booking_service import AvailabilityResult
        return AvailabilityResult(True, "That slot is available.")

    monkeypatch.setattr(xai_realtime, "check_availability_only", check)
    output = await voice_session._run_check_availability(
        json.dumps({"requested_start_iso": "2026-09-04T14:00:00Z"})
    )

    assert json.loads(output)["available"] is True
    assert len(checked) == 1
    assert voice_session.session.appointment_id is None
    assert voice_session.session.booking_status == "awaiting_selection"


async def test_confirmation_claim_is_cancelled_without_booked_state(voice_session):
    sent = []

    async def capture(payload):
        sent.append(payload)

    voice_session._send = capture
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        {"type": "response.output_audio_transcript.delta", "data": {"delta": "Your appointment is confirmed."}}
    )

    assert any(payload["type"] == "response.cancel" for payload in sent)
    assert "could not complete" in voice_session._pending_agent.lower()
    assert voice_session._pending_forced_tool_message is not None


async def test_already_booked_conflict_speech_is_not_cancelled(voice_session):
    """After Square says the slot is taken, the agent must be allowed to say so.

    Live failure: bare 'booked'/'confirmed' cancelled TTS mid-sentence, which
    sounded like crackle and then silence."""
    sent = []

    async def capture(payload):
        sent.append(payload)

    voice_session._send = capture
    voice_session.session.booking_status = "conflict"
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        {
            "type": "response.output_audio_transcript.delta",
            "data": {"delta": "That time is already booked. I can check another opening."},
        }
    )

    assert not any(payload.get("type") == "response.cancel" for payload in sent)
    assert "already booked" in voice_session._pending_agent.lower()


def test_call_session_booking_state_round_trips():
    session = CallSession(
        "call-state", "inbound", "+15550001", "+15550002",
        booking_status="booked", appointment_id="appointment-1",
        confirmed_datetime="2026-09-04T14:00:00+00:00",
    )

    restored = CallSession.from_dict(session.to_dict())

    assert restored.booking_status == "booked"
    assert restored.appointment_id == "appointment-1"


def test_several_minutes_of_english_caller_speech_defaults_to_english():
    session = CallSession("language", "inbound", "+1", "+2")
    session.add_turn("user", "I would like to book a massage for tomorrow, please.")

    assert primary_caller_language(session, "Not detected") == "English"


def test_successful_booking_state_requires_persisted_appointment():
    session = CallSession("booking-state", "inbound", "+1", "+2")
    appointment = type("Appointment", (), {
        "id": uuid.uuid4(),
        "start_time": datetime(2026, 9, 4, 16, tzinfo=timezone.utc),
        "external_booking_id": "external-1",
    })()

    apply_booking_result(session, BookingResult(BookingOutcome.BOOKED, appointment=appointment))

    assert session.booking_status == "booked"
    assert session.appointment_id == str(appointment.id)
    assert session.external_booking_id == "external-1"


def test_booked_without_appointment_cannot_enable_confirmation():
    session = CallSession("bad-booking-state", "inbound", "+1", "+2")

    apply_booking_result(session, BookingResult(BookingOutcome.BOOKED))

    assert session.booking_status == "failed"
    assert session.appointment_id is None


def test_twilio_reply_cannot_confirm_without_persisted_booking():
    session = CallSession("reply", "inbound", "+1", "+2")

    reply = _authoritative_reply(session, "Perfect, your appointment is confirmed.")

    assert "could not complete" in reply.lower()


def test_twilio_reply_allows_already_booked_conflict_language():
    session = CallSession("reply-conflict", "inbound", "+1", "+2")
    session.booking_status = "conflict"
    reply = _authoritative_reply(
        session, "That time is already booked. Would you like a later opening?"
    )
    assert "already booked" in reply.lower()


async def test_booking_outcome_is_recorded_in_the_transcript(voice_session, booking_stub):
    output = await voice_session._run_manage_appointment(json.dumps({"intent": "schedule"}))
    payload = json.loads(output)
    assert payload["booked"] is False
    assert booking_stub == []
    system_turns = [t for t in voice_session.session.history if t["role"] == "system"]
    assert system_turns == []


async def test_malformed_tool_arguments_ask_the_caller_to_restate(voice_session, booking_stub):
    """A bad tool payload must not drop the call — the agent should recover by
    asking again."""
    output = await voice_session._run_manage_appointment("{not json")
    payload = json.loads(output)
    assert payload["booked"] is False
    assert booking_stub == []


async def test_invalid_intent_value_is_reported_not_raised(voice_session, booking_stub):
    output = await voice_session._run_manage_appointment(json.dumps({"nonsense": True}))
    assert output
    assert booking_stub == []


async def test_function_call_event_returns_output_and_requests_speech(voice_session, booking_stub):
    """After a tool result the model needs an explicit `response.create`, or the
    caller hears nothing following the booking."""
    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    voice_session._send = _capture

    await voice_session._dispatch(
        {
            "type": "response.function_call_arguments.done",
            "data": {
                "name": "manage_appointment",
                "call_id": "fc_1",
                "arguments": json.dumps({"intent": "schedule"}),
            },
        }
    )

    assert [p["type"] for p in sent] == ["conversation.item.create", "response.create"]
    item = sent[0]["item"]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "fc_1"
    payload = json.loads(item["output"])
    assert payload["booked"] is False


async def test_unknown_tool_is_refused_without_touching_the_booking_engine(
    voice_session, booking_stub
):
    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    voice_session._send = _capture

    await voice_session._dispatch(
        {
            "type": "response.function_call_arguments.done",
            "data": {"name": "drop_database", "call_id": "fc_2", "arguments": "{}"},
        }
    )
    assert booking_stub == []
    assert "not available" in sent[0]["item"]["output"]


def test_tool_schema_matches_the_appointment_intent_fields():
    """The tool's arguments are fed straight into AppointmentIntent, so a field
    that does not exist there would fail validation on every booking."""
    from app.services.grok_service import AppointmentIntent

    allowed = set(AppointmentIntent.model_fields)
    exposed = set(MANAGE_APPOINTMENT_TOOL["parameters"]["properties"])
    assert exposed <= allowed


# --------------------------------------------------------------------------- #
# Greeting
# --------------------------------------------------------------------------- #
async def test_greeting_is_spoken_on_connect(voice_session):
    """The reported fault was silence until the caller said "hello"; the agent
    does not open on its own, so we must push the greeting ourselves."""
    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    voice_session._send = _capture
    await voice_session._greet()

    assert len(sent) == 1
    item = sent[0]["item"]
    assert item["type"] == "force_message"
    assert item["content"][0]["text"] == (
        "Thank you for calling Healing Waters Day Spa. "
        "I’m Cara, your AI receptionist. May I help you reserve an appointment today?"
    )
    assert voice_session.session.greeting_requested is True
    assert voice_session.session.greeting_sent is False
    await voice_session._dispatch({"type": "response.created", "response": {"id": "greet-1"}})
    await voice_session._dispatch(
        {
            "type": "response.output_audio.delta",
            "response": {"id": "greet-1"},
            "delta": "AA==",
        }
    )
    assert voice_session.session.greeting_sent is True
    await voice_session._greet()
    assert len(sent) == 1, "a second _greet() on the same CallSid must be a no-op"


async def test_greeting_falls_back_to_model_when_opening_force_message_errors(voice_session):
    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    voice_session._send = _capture
    voice_session.session.greeting_sent = False
    await voice_session._dispatch({"type": "error", "data": {"message": "unknown item type"}})

    assert [p["type"] for p in sent] == ["conversation.item.create", "response.create"]
    assert sent[0]["item"]["type"] == "message"
    assert voice_session.session.greeting_sent is False
    await voice_session._dispatch({"type": "response.created", "response": {"id": "greet-fb"}})
    await voice_session._dispatch(
        {
            "type": "response.output_audio.delta",
            "response": {"id": "greet-fb"},
            "delta": "AA==",
        }
    )
    assert voice_session.session.greeting_sent is True


async def test_untyped_error_frame_is_recognised(voice_session):
    """Observed on a live socket: xAI can send a bare {"error": "..."} frame
    with no `type` field. Treating it as unhandled would hide real failures."""
    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    voice_session._send = _capture
    await voice_session._greet()
    await voice_session._dispatch({"type": "response.created", "response": {"id": "greet-1"}})
    await voice_session._dispatch(
        {
            "type": "response.output_audio.delta",
            "response": {"id": "greet-1"},
            "delta": "AA==",
        }
    )
    sent.clear()
    voice_session._user_turn_count = 2

    await voice_session._dispatch({"error": "call_id not found or no longer valid"})

    assert sent == []
    assert all(
        (p.get("item") or {}).get("content", [{}])[0].get("text") != "[The caller has just connected. Greet them.]"
        for p in sent
    )


async def test_midcall_error_does_not_retrigger_greeting(voice_session):
    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    voice_session._send = _capture
    await voice_session._greet()
    await voice_session._dispatch({"type": "response.created", "response": {"id": "greet-1"}})
    await voice_session._dispatch(
        {
            "type": "response.output_audio.delta",
            "response": {"id": "greet-1"},
            "delta": "AA==",
        }
    )
    sent.clear()
    voice_session._user_turn_count = 3
    voice_session.session.greeting_sent = True

    await voice_session._dispatch({"type": "error", "data": {"message": "boom"}})

    assert sent == []
    assert "[The caller has just connected. Greet them.]" not in json.dumps(sent)


async def test_greeting_fallback_happens_at_most_once(voice_session):
    sent: list = []

    async def _capture(payload):
        sent.append(payload)

    voice_session._send = _capture
    voice_session.session.greeting_sent = False

    for _ in range(3):
        await voice_session._dispatch({"type": "error", "data": {"message": "boom"}})

    assert [p["type"] for p in sent] == ["conversation.item.create", "response.create"]
