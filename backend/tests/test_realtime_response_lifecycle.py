"""Regression coverage for a real live-call failure (2026-09-24, call
CAfde2d0d5979e073815c54877f6c9a4ff, "Your Day Spa", HydroLux5 Facial - Face
Only).

Traced from the actual Railway logs of that call:

  * Eight `check_availability` tool calls landed within a couple of seconds,
    all sharing the same `response_id`, with the model fabricating a new
    30-minutes-apart candidate start time for each one
    ("2026-09-24T10:15:00", "10:30:00", "11:00:00", ... "17:00:00") instead
    of asking the caller or using one real backend search. Nothing capped
    how many of these one turn could trigger.
  * `_handle_function_call` sent `response.create` after EVERY one of those
    eight tool results, while xAI was still emitting more function calls
    under the SAME response_id — stacking new response generations on top
    of a response that was not done yet. The queue-depth warnings
    (33 -> 46 -> 76 -> 93) climbed during exactly this window.
  * "Cancellation failed: no active response found" appeared repeatedly,
    including once immediately after this tool-call burst, from
    `response.cancel` being sent unconditionally (on caller barge-in, and
    from the booking/availability guards) with no tracking of whether a
    response was actually open.

None of this is provable from the backend logs alone: ordinary spoken text
isn't logged, so there is no literal server-side proof that a specific
`available=True` result was voiced as "unavailable". What IS provable, and
what these tests cover, is the mechanism that produced the failure pattern:
an unbounded chain of self-guessed availability probes in one turn, and an
unguarded response/cancel lifecycle that let a stale response's tool call
still run and let redundant response.create calls stack on an open response.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.appointment_booking_service import AvailabilityResult
from app.services.xai_realtime import CHECK_AVAILABILITY_TOOL, XAIVoiceSession
from app.services.call_state import CallSession


def _tomorrow_iso(hour: int, minute: int = 0) -> str:
    """A local-time ISO string for tomorrow, computed relative to the real
    clock so these tests never go stale (the code under test resolves
    "tomorrow" the same way, off `datetime.now()`)."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute).isoformat()


def _session() -> CallSession:
    return CallSession(
        "call-1", "inbound", "+15550000002", "+15550000001",
        history=[{"role": "assistant", "content": "Thank you for calling Healing Waters Day Spa."}],
        business_name="Healing Waters Day Spa",
    )


@pytest.fixture
def voice_session(monkeypatch):
    voice = XAIVoiceSession("call-1", _session())

    async def _noop() -> None:
        return None

    monkeypatch.setattr(voice, "_persist_session", _noop)
    return voice


async def _caller_says(voice_session, text: str) -> None:
    """Simulate one completed caller utterance — the only thing that can
    ground the NEXT exact-time probe (see `_is_time_grounded`)."""
    await voice_session._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": text},
        }
    )


def _call_done_event(*, response_id: str, call_id: str, start_iso: str) -> dict:
    return {
        "type": "response.function_call_arguments.done",
        "response_id": response_id,
        "call_id": call_id,
        "name": CHECK_AVAILABILITY_TOOL["name"],
        "arguments": json.dumps({
            "requested_start_iso": start_iso,
            "service_description": "60 minute HydroLux5 Facial - Face Only",
        }),
    }


@pytest.fixture
def availability_stub(monkeypatch):
    """Every probe reports genuinely available — isolates the loop/response
    guard from availability outcome, matching the live call where several of
    the guessed times really were free."""
    from app.services import xai_realtime

    calls: list[str] = []

    async def _check(_db, _session, intent):
        calls.append(intent.requested_start_iso)
        return AvailabilityResult(True, "That time is available.")

    monkeypatch.setattr(xai_realtime, "check_availability_only", _check)
    return calls


async def test_one_response_cannot_chain_an_unbounded_run_of_availability_probes(
    voice_session, availability_stub, monkeypatch
):
    """The exact live pattern: eight self-guessed times, one response_id.
    Only the first may reach the provider; the rest must be refused without
    ever calling Square, and the refusal must tell the model to stop."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)
    await _caller_says(voice_session, "I'd like to book the HydroLux5 facial, Face Only, at 10:15 am tomorrow.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-storm"}})

    guessed_times = [
        "2026-09-24T10:15:00", "2026-09-24T10:30:00", "2026-09-24T11:00:00",
        "2026-09-24T11:30:00", "2026-09-24T12:00:00", "2026-09-24T12:30:00",
        "2026-09-24T13:00:00", "2026-09-24T13:30:00",
    ]
    for i, start in enumerate(guessed_times):
        await voice_session._dispatch(
            _call_done_event(response_id="resp-storm", call_id=f"call-{i}", start_iso=start)
        )

    assert len(availability_stub) == 1, "only the first probe in one response may reach the provider"

    outputs = [
        json.loads(item["item"]["output"])
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    assert outputs[0]["status"] == "available"
    for refused in outputs[1:]:
        # The temporal-grounding guard (turn-scoped) catches these before the
        # per-response cap even gets a chance to; the hard chain-depth cap is
        # a further-out backstop that can also catch the tail of a long
        # enough storm. Either is an acceptable rejection category here —
        # what matters is NONE of them reached the provider.
        assert refused["status"] in {"ungrounded_time", "tool_chain_limit"}
        assert refused["available"] is False


async def test_per_response_cap_still_refuses_a_second_grounded_probe_in_one_response(
    voice_session, availability_stub, monkeypatch
):
    """The per-response cap is a secondary safety net, independent of
    grounding: even if the caller genuinely spoke twice before the response
    closed, only one availability probe per response may reach the provider."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})

    first_time = _tomorrow_iso(10, 15)
    second_time = _tomorrow_iso(11, 0)
    await _caller_says(voice_session, "Do you have anything at 10:15 am tomorrow?")
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-a", start_iso=first_time)
    )
    # A second, genuinely new caller turn — grounded on its own — but still
    # inside the same still-open response.
    await _caller_says(voice_session, "Or actually, what about 11 am instead?")
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-b", start_iso=second_time)
    )

    assert availability_stub == [first_time]
    outputs = [
        json.loads(item["item"]["output"])
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    assert outputs[1]["status"] == "too_many_attempts"


async def test_a_new_response_id_does_not_reset_permission_to_keep_guessing(
    voice_session, availability_stub, monkeypatch
):
    """The per-response cap alone is not the correctness rule: response A
    invents 10am, response B invents 11am, response C invents noon — each
    individually obeys the one-probe-per-response cap, but none of them
    followed a new caller turn that actually named a time. All but the very
    first (which at least followed a real, time-mentioning turn) must be
    refused, regardless of how many different response_ids are used to
    spread the guesses across."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)
    await _caller_says(voice_session, "Book the HydroLux5 Face Only for 10 am tomorrow.")

    for i, (response_id, start) in enumerate([
        ("resp-A", "2026-09-24T10:00:00"),
        ("resp-B", "2026-09-24T11:00:00"),
        ("resp-C", "2026-09-24T12:00:00"),
    ]):
        await voice_session._dispatch({"type": "response.created", "response": {"id": response_id}})
        await voice_session._dispatch(
            _call_done_event(response_id=response_id, call_id=f"call-{i}", start_iso=start)
        )
        await voice_session._dispatch({"type": "response.done", "response": {"id": response_id}})

    assert availability_stub == ["2026-09-24T10:00:00"], (
        "only the first, turn-grounded probe may reach the provider — a new "
        "response_id must not re-grant permission to guess"
    )
    outputs = [
        json.loads(item["item"]["output"])
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    assert outputs[0]["status"] == "available"
    assert outputs[1]["status"] == "ungrounded_time"
    assert outputs[2]["status"] == "ungrounded_time"


async def test_response_create_is_not_sent_while_the_same_response_is_still_open(
    voice_session, availability_stub, monkeypatch
):
    """The other half of the same live bug: xAI can emit several function
    calls under one still-open response. `response.create` must fire at most
    once while that response_id remains active — not once per tool call —
    or new response generations stack on top of one still producing audio."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)
    await _caller_says(voice_session, "Do you have anything free tomorrow around 10?")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-storm"}})

    for hour, minute in [(10, 15), (10, 30), (11, 0)]:
        await voice_session._dispatch(
            _call_done_event(
                response_id="resp-storm",
                call_id=f"call-{hour}-{minute}",
                start_iso=_tomorrow_iso(hour, minute),
            )
        )

    response_create_count = sum(1 for item in sent if item.get("type") == "response.create")
    assert response_create_count == 0, (
        "no response.create should fire while resp-storm is still open"
    )

    # Once that response actually finishes, its deferred continuation fires —
    # restricted to speech-only, since two of the three tool results in it
    # were rejections (the point of this whole fix: the model must be able
    # to tell the caller something, but not retry a tool autonomously).
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-storm"}})
    creates = [item for item in sent if item.get("type") == "response.create"]
    assert len(creates) == 1
    assert creates[0].get("response", {}).get("tool_choice") == "none"

    # A genuinely new, grounded caller turn may prompt an unrestricted one.
    await _caller_says(voice_session, "How about 2pm instead?")
    await voice_session._dispatch(
        _call_done_event(response_id="resp-next", call_id="call-next", start_iso=_tomorrow_iso(14))
    )
    creates = [item for item in sent if item.get("type") == "response.create"]
    assert len(creates) == 2
    assert "response" not in creates[1] or creates[1]["response"].get("tool_choice") != "none"


async def test_duplicate_call_id_is_not_executed_twice(voice_session, availability_stub, monkeypatch):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "Do you have anything at 10:15 am tomorrow?")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    event = _call_done_event(response_id="resp-1", call_id="call-dup", start_iso="2026-09-24T10:15:00")
    await voice_session._dispatch(event)
    await voice_session._dispatch(dict(event))  # xAI redelivers the same call_id

    assert len(availability_stub) == 1
    outputs = [
        item["item"]["output"]
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    assert len(outputs) == 2, "the duplicate must still receive a function_call_output"
    assert outputs[0] == outputs[1]


async def test_stale_response_function_call_is_ignored_after_cancellation(
    voice_session, availability_stub, monkeypatch
):
    """A function call belonging to a response we already cancelled (the
    booking-confirmation guard, the availability-claim guard, or a caller
    barge-in) must not still reach the provider."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-cancel-me"}})
    # The confirmation-claim guard fires and cancels the active response.
    await voice_session._dispatch(
        {"type": "response.output_audio_transcript.delta", "data": {"delta": "Your appointment is confirmed."}}
    )
    assert any(item.get("type") == "response.cancel" for item in sent)

    # A function call that was already in flight for that (now cancelled)
    # response still arrives afterward.
    await voice_session._dispatch(
        _call_done_event(response_id="resp-cancel-me", call_id="call-stale", start_iso="2026-09-24T10:15:00")
    )

    assert availability_stub == [], "a stale/cancelled response's tool call must never reach the provider"


async def test_response_cancel_is_not_sent_when_nothing_is_active(voice_session, monkeypatch):
    """The other reported symptom: 'Cancellation failed: no active response
    found'. Caused by sending response.cancel unconditionally. Guard it."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    # No response.created was ever dispatched — nothing is active.
    await voice_session._dispatch(
        {"type": "response.output_audio_transcript.delta", "data": {"delta": "Your appointment is confirmed."}}
    )

    assert not any(item.get("type") == "response.cancel" for item in sent)
    # The guard's fallback wording still applies regardless.
    assert "could not complete" in voice_session._pending_agent.lower()


# --------------------------------------------------------------------------- #
# B/E/F: date-only (no time) is refused; an explicit time, or picking a
# previously-offered slot, is allowed.
# --------------------------------------------------------------------------- #
async def test_date_only_no_time_of_day_mentioned_is_refused_not_guessed(
    voice_session, availability_stub, monkeypatch
):
    """"Tomorrow" names a date but no hour. Even though it is a fresh caller
    turn, an exact timestamp built from it would still have a fabricated
    time-of-day — the model must be told to ask, not to invent one."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)
    await _caller_says(voice_session, "Tomorrow.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-09-24T09:00:00")
    )

    assert availability_stub == []
    outputs = [
        json.loads(item["item"]["output"])
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    assert outputs[0]["status"] == "ungrounded_time"


async def test_explicit_date_and_time_is_allowed(voice_session, availability_stub, monkeypatch):
    """"Tomorrow at 2pm" names both — this is exactly what the guard exists
    to let through."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)
    await _caller_says(voice_session, "Tomorrow at 2pm, please.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-09-24T14:00:00")
    )

    assert availability_stub == ["2026-09-24T14:00:00"]
    outputs = [
        json.loads(item["item"]["output"])
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    assert outputs[0]["status"] == "available"


async def test_selecting_a_previously_offered_slot_is_allowed_without_a_time_mention(
    voice_session, availability_stub, monkeypatch
):
    """"The second one" names no hour in the caller's own words, but it
    exactly matches a slot already offered — that is real evidence too, not
    a guess, so it must be let through even without a fresh time-mention."""
    from app.services.booking_state import get_draft, save_draft

    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    draft = get_draft(voice_session.session)
    draft.alternative_slots = [
        {
            "start": "2026-09-24T14:00:00Z",
            "location_id": "loc_123",
            "team_member_id": "TM1",
            "service_variation_id": "var1",
            "service_variation_version": 1,
            "duration_minutes": 60,
            "provider": "square",
        }
    ]
    save_draft(voice_session.session, draft)

    await _caller_says(voice_session, "The second one, please.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-09-24T14:00:00Z")
    )

    assert availability_stub == ["2026-09-24T14:00:00Z"]
    outputs = [
        json.loads(item["item"]["output"])
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    assert outputs[0]["status"] == "available"
