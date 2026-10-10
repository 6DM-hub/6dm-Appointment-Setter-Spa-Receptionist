"""Regressions for booking details surviving Cara's multi-turn consultation.

The live failure behind these tests started with a caller selecting a
60-minute Swedish massage, provider SIX, and a calendar day.  By the time the
caller answered the consultation and part-of-day questions, the realtime
model sent durationless ``Swedish Massage`` tool arguments.  That changed a
resolved request back into an ambiguous service and made Cara appear to forget
both what and when the caller requested.

The model's next tool call is deliberately treated as incomplete input here.
The backend-owned session state must carry the confirmed consultation choice
and caller-grounded date across as many conversational turns as necessary.
"""

import json
import re
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.booking_state import get_draft, save_draft
from app.services.call_state import CallSession
from app.services.xai_realtime import XAIVoiceSession
from tests.conftest import make_spa


def _voice() -> XAIVoiceSession:
    session = CallSession(
        "call-request-memory",
        "inbound",
        "+15550000002",
        "+15550000001",
        business_name="Test Spa",
        tenant_id="tenant-1",
    )
    voice = XAIVoiceSession(
        "call-request-memory",
        session,
        now_provider=lambda tz: datetime(2026, 10, 10, 12, 0, tzinfo=tz),
    )
    # These are state-machine tests.  Keep them independent from Redis and a
    # live xAI socket when transcription completion persists the turn.
    voice._persist_session = AsyncMock()
    voice._send = AsyncMock()
    return voice


async def _caller_says(voice: XAIVoiceSession, text: str) -> None:
    await voice._dispatch(
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "data": {"transcript": text},
        }
    )


@pytest.mark.parametrize(
    "selected_service",
    ["Swedish Massage", "60 Minute Swedish Massage"],
)
def test_consultation_duration_qualifies_later_durationless_availability_args(
    selected_service,
):
    """A separately selected duration remains attached to the same service.

    Realtime tool arguments are model output, so a later durationless label
    cannot override the structured choice already made with the caller.
    """
    voice = _voice()
    voice.session.entities["consultation_state"] = {
        "kind": "massage",
        "selected_service": selected_service,
        "selected_duration_minutes": 60,
    }
    args = {
        "requested_services": ["Swedish Massage"],
        "service_description": "Swedish Massage",
        "preferred_staff": "SIX",
        "requested_start_iso": "2026-10-14T14:00:00",
    }

    voice._coalesce_service_arguments(args)

    assert len(args["requested_services"]) == 1
    restored = args["requested_services"][0]
    assert "swedish massage" in restored.casefold()
    assert re.search(r"\b60\b", restored)
    assert args["service_description"] == restored
    assert args["preferred_staff"] == "SIX"
    assert args["requested_start_iso"] == "2026-10-14T14:00:00"


def test_consultation_duration_never_leaks_to_a_different_service():
    """Stale massage state must not turn a new facial into a 60-minute one."""
    voice = _voice()
    voice.session.entities["consultation_state"] = {
        "kind": "massage",
        "selected_service": "Swedish Massage",
        "selected_duration_minutes": 60,
    }
    args = {
        "requested_services": ["European Facial"],
        "service_description": "European Facial",
        "preferred_staff": "ESTHETICIAN",
        "requested_start_iso": "2026-10-14T14:00:00",
    }

    voice._coalesce_service_arguments(args)

    assert args["requested_services"] == ["European Facial"]
    assert args["service_description"] == "European Facial"
    assert args["preferred_staff"] == "ESTHETICIAN"
    assert args["requested_start_iso"] == "2026-10-14T14:00:00"


@pytest.mark.asyncio
async def test_later_consultation_lookup_cannot_erase_selected_service_or_duration(
    monkeypatch,
):
    """Answer collection after a choice may add fields, never erase the choice."""
    from app.services import xai_realtime

    class DBContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    async def fake_lookup(_spa, **kwargs):
        selected_name = kwargs.get("selected_service_name")
        selected_minutes = kwargs.get("selected_duration_minutes")
        return {
            "status": "known",
            "message": "Consultation result.",
            "consultation": {
                "category": "relaxation",
                "service": {"name": "Swedish Massage"},
                "selected_service": (
                    {
                        "name": selected_name,
                        "duration_minutes": selected_minutes,
                    }
                    if selected_name and selected_minutes
                    else None
                ),
                "durations": {"choices": [{"minutes": 30}, {"minutes": 60}]},
                "addons": [],
            },
        }

    spa = make_spa(
        services=[
            {
                "name": "Swedish Massage",
                "duration_minutes": 60,
                "consultation_kind": "massage",
                "consultation_category": "relaxation",
            }
        ]
    )
    monkeypatch.setattr(xai_realtime, "AsyncSessionLocal", lambda: DBContext())
    monkeypatch.setattr(
        xai_realtime,
        "_prepare",
        AsyncMock(return_value=SimpleNamespace(spa=spa, adapter=None)),
    )
    monkeypatch.setattr(xai_realtime, "lookup_spa_facts", fake_lookup)

    voice = _voice()
    voice._persist_session = AsyncMock()
    await voice._run_lookup_spa_facts(
        json.dumps(
            {
                "topic": "consultation",
                "consultation_kind": "massage",
                "selected_service_name": "Swedish Massage",
                "selected_duration_minutes": 60,
            }
        )
    )
    await voice._run_lookup_spa_facts(
        json.dumps(
            {
                "topic": "consultation",
                "consultation_kind": "massage",
                "massage_reason": "relaxation",
                "pressure_preference": "medium",
                "safety_answered": True,
            }
        )
    )

    state = voice.session.entities["consultation_state"]
    assert state["selected_service"] == "Swedish Massage"
    assert state["selected_duration_minutes"] == 60
    assert state["category"] == "relaxation"


@pytest.mark.asyncio
async def test_requested_day_survives_more_than_recent_history_consultation_window():
    """The requested day is structured caller evidence, not short-term text memory."""
    voice = _voice()

    await _caller_says(
        voice,
        "I want a 60 minute Swedish massage with SIX next Wednesday.",
    )
    # More turns than the historical transcript lookback. None changes the day.
    for answer in (
        "Mostly relaxation.",
        "My shoulders.",
        "Medium pressure.",
        "No injuries.",
        "The 60 minute option.",
        "No add-ons, thank you.",
    ):
        await _caller_says(voice, answer)
    await _caller_says(voice, "Afternoon works best.")

    assert voice._active_established_date() == date(2026, 10, 14)
    window = voice._spoken_day_part_window()
    assert window is not None
    start, end = window
    assert start.date() == date(2026, 10, 14)
    assert (start.hour, end.hour) == (12, 17)
    remembered = voice.session.entities["caller_grounded_requested_date"]
    assert remembered == {"date": "2026-10-14", "source": "weekday"}


@pytest.mark.asyncio
async def test_date_correction_replaces_memory_and_retraction_clears_it():
    voice = _voice()

    await _caller_says(voice, "Next Wednesday, please.")
    assert voice._active_established_date() == date(2026, 10, 14)

    await _caller_says(voice, "Actually, make that Thursday.")
    assert voice._active_established_date() == date(2026, 10, 15)

    await _caller_says(voice, "Never mind that date.")
    assert voice._active_established_date() is None
    assert voice.session.entities["caller_grounded_requested_date"] == {
        "date": None,
        "source": "cleared",
    }


def test_grounded_exact_time_survives_session_serialization_and_voice_rebuild():
    """A provider retry after Redis/worker restoration retains only that instant."""
    voice = _voice()
    chosen = "2026-10-14T14:00:00-05:00"
    different = "2026-10-14T14:30:00-05:00"
    voice._remember_grounded_exact_time(chosen)

    # Exercise the same JSON round trip used by CallStateStore/Redis, then
    # construct a new realtime driver as happens after state restoration.
    restored_session = CallSession.from_dict(
        json.loads(json.dumps(voice.session.to_dict()))
    )
    restored_voice = XAIVoiceSession(
        "call-request-memory",
        restored_session,
        now_provider=lambda tz: datetime(2026, 10, 10, 12, 0, tzinfo=tz),
    )

    assert restored_voice._is_time_grounded(chosen)
    assert not restored_voice._is_time_grounded(different)
    assert restored_session.entities["caller_grounded_exact_times"] == [
        "2026-10-14T19:00:00+00:00"
    ]


@pytest.mark.asyncio
async def test_consultation_tool_continuation_does_not_clear_staged_request(monkeypatch):
    """A fact lookup/continuation cannot reset a provider, service, or time."""
    from app.services import xai_realtime

    class DBContext:
        async def __aenter__(self):
            return SimpleNamespace()

        async def __aexit__(self, *_args):
            return False

    spa = make_spa(services=[])
    monkeypatch.setattr(xai_realtime, "AsyncSessionLocal", lambda: DBContext())
    monkeypatch.setattr(
        xai_realtime,
        "_prepare",
        AsyncMock(return_value=SimpleNamespace(spa=spa, adapter=None)),
    )

    voice = _voice()
    voice._persist_session = AsyncMock()
    draft = get_draft(voice.session)
    draft.service_description = "60 minute Swedish Massage"
    draft.preferred_staff = "SIX"
    draft.start_iso = "2026-10-14T14:00:00-05:00"
    draft.end_iso = "2026-10-14T15:00:00-05:00"
    save_draft(voice.session, draft)

    await voice._run_lookup_spa_facts(
        json.dumps(
            {
                "topic": "consultation",
                "consultation_kind": "massage",
                "massage_reason": "relaxation",
            }
        )
    )

    retained = get_draft(voice.session)
    assert retained.service_description == "60 minute Swedish Massage"
    assert retained.preferred_staff == "SIX"
    assert retained.start_iso == "2026-10-14T14:00:00-05:00"
    assert retained.end_iso == "2026-10-14T15:00:00-05:00"

