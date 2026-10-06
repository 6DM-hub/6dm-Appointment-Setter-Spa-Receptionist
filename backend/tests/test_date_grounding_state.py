"""Whether a bare "3 PM" turn may complete an EARLIER date depends on that
date still being the active one — not merely "mentioned somewhere in the
last few turns". No structured booking-draft field tracks "the date the
caller is currently discussing" (the draft only ever holds a complete
start_iso once a full request has been staged); `_active_established_date`
in xai_realtime.py is a transcript heuristic, not authoritative state. These
tests pin its actual behavior: the most recent date-naming turn wins outright
(letting a correction supersede an older date), and an explicit cancellation
with no date in the same breath clears it rather than falling back further.
"""
import json
from datetime import datetime, time, timedelta

import pytest

from app.services.appointment_booking_service import (
    AvailabilityResult,
    BookingOutcome,
    BookingResult,
)
from app.services.xai_realtime import CHECK_AVAILABILITY_TOOL, XAIVoiceSession
from app.services.call_state import CallSession


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
    from app.services import xai_realtime

    calls: list[str] = []

    async def _check(_db, _session, intent):
        calls.append(intent.requested_start_iso)
        return AvailabilityResult(True, "That time is available.")

    monkeypatch.setattr(xai_realtime, "check_availability_only", _check)
    return calls


def _last_status(sent: list[dict]) -> str:
    outputs = [
        json.loads(item["item"]["output"])
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]
    return outputs[-1]["status"]


async def _probe(voice_session, sent, response_id, call_id, start_iso):
    await voice_session._dispatch({"type": "response.created", "response": {"id": response_id}})
    await voice_session._dispatch(
        _call_done_event(response_id=response_id, call_id=call_id, start_iso=start_iso)
    )
    await voice_session._dispatch({"type": "response.done", "response": {"id": response_id}})


# --------------------------------------------------------------------------- #
# A. date stays active across a later time-only turn
# --------------------------------------------------------------------------- #
async def test_a_date_named_then_time_only_turn_resolves_that_date(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "October 1st.")
    await _caller_says(voice_session, "3 PM.")
    await _probe(voice_session, sent, "resp-1", "call-1", "2026-10-01T15:00:00")

    assert _last_status(sent) == "available"
    assert availability_stub == ["2026-10-01T15:00:00"]


# --------------------------------------------------------------------------- #
# B. caller corrects the date; the correction wins, not the original
# --------------------------------------------------------------------------- #
async def test_b_corrected_date_resolves_to_the_new_date_not_the_original(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "October 1st.")
    await _caller_says(voice_session, "Actually make that October 2nd.")
    await _caller_says(voice_session, "3 PM.")

    # The model correctly proposes October 2nd (what a real model would
    # combine from the caller's own most recent correction) — must be
    # accepted.
    await _probe(voice_session, sent, "resp-1", "call-1", "2026-10-02T15:00:00")
    assert _last_status(sent) == "available"
    assert availability_stub == ["2026-10-02T15:00:00"]


async def test_b_the_superseded_original_date_is_no_longer_grounded(
    voice_session, availability_stub, monkeypatch
):
    """Same conversation as above, but the model (wrongly) tries to combine
    "3 PM" with the STALE October 1st instead of the correction. Must be
    refused, not silently booked on the wrong day."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "October 1st.")
    await _caller_says(voice_session, "Actually make that October 2nd.")
    await _caller_says(voice_session, "3 PM.")

    await _probe(voice_session, sent, "resp-1", "call-1", "2026-10-01T15:00:00")
    assert _last_status(sent) == "ungrounded_time"
    assert availability_stub == []


# --------------------------------------------------------------------------- #
# C. caller cancels the date outright; it must not be resurrected
# --------------------------------------------------------------------------- #
async def test_c_cancelled_date_is_not_resurrected_for_a_later_time_only_turn(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "October 1st.")
    await _caller_says(voice_session, "Never mind that date.")
    await _caller_says(voice_session, "3 PM.")

    await _probe(voice_session, sent, "resp-1", "call-1", "2026-10-01T15:00:00")

    assert _last_status(sent) == "ungrounded_time"
    assert availability_stub == []


# --------------------------------------------------------------------------- #
# D. a date mentioned conversationally (not as part of a booking request)
# --------------------------------------------------------------------------- #
async def test_d_a_conversational_date_mention_cannot_be_distinguished_from_a_booking_one(
    voice_session, availability_stub, monkeypatch
):
    """Documents a real, known limitation rather than asserting a design we
    don't actually have: nothing in this architecture tags a date mention
    with "this is a booking request" vs "this was incidental smalltalk"
    (e.g. "my birthday is October 1st"). The heuristic here only recognizes
    presence of a date-like phrase, so this conversational date is treated
    the same as a booking-intent one. Fixing this would need real intent
    classification of the utterance, not a bigger regex — out of scope for
    this fix. This test exists so a future change to that behavior is a
    deliberate decision, not a silent regression."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "By the way, my birthday is October 1st.")
    await _caller_says(voice_session, "3 PM.")

    await _probe(voice_session, sent, "resp-1", "call-1", "2026-10-01T15:00:00")

    # Current, documented behavior: the conversational mention still grounds
    # the probe. If this changes, update this test deliberately.
    assert _last_status(sent) == "available"


def _next_local(voice_session, weekday: int, hour: int, minute: int = 0) -> datetime:
    """Same rule the voice session uses: next weekday at this clock, or +7 if past."""
    now = datetime.now(voice_session._tz)
    days = (weekday - now.date().weekday()) % 7
    start = datetime.combine(
        now.date() + timedelta(days=days),
        time(hour, minute),
        tzinfo=voice_session._tz,
    )
    if start <= now:
        start += timedelta(days=7)
    return start


async def test_thursday_at_4pm_rewrites_a_wrong_model_timestamp(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)
    expected = _next_local(voice_session, 3, 16).strftime("%Y-%m-%dT%H:%M:%S")

    await _caller_says(voice_session, "Thursday, 4pm")
    await _probe(voice_session, sent, "resp-1", "call-1", "2020-01-01T09:00:00")

    assert _last_status(sent) == "available"
    assert availability_stub == [expected]


async def test_saturday_then_4pm_uses_that_saturday(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)
    expected = _next_local(voice_session, 5, 16).strftime("%Y-%m-%dT%H:%M:%S")

    await _caller_says(voice_session, "Saturday")
    await _caller_says(voice_session, "4pm")
    await _probe(voice_session, sent, "resp-1", "call-1", "1999-05-01T16:00:00")

    assert _last_status(sent) == "available"
    assert availability_stub == [expected]


async def test_saturday_afternoon_searches_the_window_not_one_minute(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []
    windows: list[tuple[datetime, datetime]] = []

    async def capture(payload):
        sent.append(payload)

    async def fake_search(_db, _session, _intent, start, end):
        windows.append((start, end))
        return BookingResult(
            BookingOutcome.CONFLICT,
            message="Openings in that part of the day: Saturday at 1:00 PM.",
        )

    monkeypatch.setattr(voice_session, "_send", capture)
    monkeypatch.setattr("app.services.xai_realtime.search_day_part", fake_search)

    now = datetime.now(voice_session._tz)
    days = (5 - now.date().weekday()) % 7
    start = datetime.combine(
        now.date() + timedelta(days=days), time(12, 0), tzinfo=voice_session._tz
    )
    end = datetime.combine(
        now.date() + timedelta(days=days), time(17, 0), tzinfo=voice_session._tz
    )
    if end <= now:
        start += timedelta(days=7)
        end += timedelta(days=7)

    await _caller_says(voice_session, "Saturday afternoon")
    await _probe(voice_session, sent, "resp-1", "call-1", "2026-10-03T14:00:00")

    assert availability_stub == []
    assert windows == [(start, end)]
    assert _last_status(sent) == "day_part_openings"
