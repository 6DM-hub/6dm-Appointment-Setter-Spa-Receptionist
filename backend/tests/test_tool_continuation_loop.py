"""Regression coverage for a real live-call failure: after a rejected
exact-time probe, the model kept retrying the SAME probe forever.

Traced root cause: `_send_function_output` nudged the model with an
unrestricted `response.create` after EVERY tool result, including our own
guard-rail rejections. Once the response that emitted the rejected probe
closed, the deferred nudge let the model open a brand new response with full
tool access and no new information — so it just called the same tool again,
got rejected again, and so on ("Uh, 3 P M." -> ungrounded_time -> deferred
response.create -> same probe -> ungrounded_time -> ...).

Two contributing bugs, both fixed here:
  * `_TIME_MENTION_RE` didn't match speech-to-text's spelled-out "3 P M."
    (space between the letters) — only contiguous "pm"/"p.m.".
  * Nothing distinguished a REJECTED tool result from a successful one when
    deciding whether the deferred continuation may call a tool again.

The fix: rejections (`ungrounded_time`, `ungrounded_earliest`,
`too_many_attempts`) mark their continuation `restrict_continuation=True`,
which sends `response.create` with `tool_choice: "none"` — the model can
only speak, never immediately retry. Two independent backstops apply even if
that field is ignored: an identical probe signature already rejected this
turn is refused outright with no further nudge at all
(`_rejected_probe_signatures_this_turn`), and a hard per-turn tool-chain
depth cap (`MAX_TOOL_CHAIN_DEPTH_PER_TURN`) bounds total tool round-trips
regardless of category.
"""
import json

import pytest

from app.services.appointment_booking_service import AvailabilityResult
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


def _call_done_event(*, response_id: str, call_id: str, start_iso: str, earliest: bool = False) -> dict:
    args: dict = {"service_description": "60 minute HydroLux5 Facial - Face Only"}
    if earliest:
        args["earliest"] = True
    else:
        args["requested_start_iso"] = start_iso
    return {
        "type": "response.function_call_arguments.done",
        "response_id": response_id,
        "call_id": call_id,
        "name": CHECK_AVAILABILITY_TOOL["name"],
        "arguments": json.dumps(args),
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


def _outputs(sent: list[dict]) -> list[dict]:
    return [
        json.loads(item["item"]["output"])
        for item in sent
        if item.get("type") == "conversation.item.create"
        and item["item"]["type"] == "function_call_output"
    ]


def _creates(sent: list[dict]) -> list[dict]:
    return [item for item in sent if item.get("type") == "response.create"]


# --------------------------------------------------------------------------- #
# Test 1 — rejected exact-time probe cannot repeat without a new caller turn
# --------------------------------------------------------------------------- #
async def test_rejected_probe_does_not_repeat_without_a_new_caller_turn(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    # The caller never named any date or time at all.
    await _caller_says(voice_session, "I'd like to book the HydroLux5 facial, Face Only.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )
    assert availability_stub == []
    assert _outputs(sent)[0]["status"] == "ungrounded_time"

    # The response closes; the deferred continuation is speech-only.
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-1"}})
    creates = _creates(sent)
    assert len(creates) == 1
    assert creates[0]["response"]["tool_choice"] == "none"

    # The model tries the exact same probe again anyway, in a new response,
    # with no new caller turn in between. It must still be refused, and no
    # further continuation may be issued for this repeat.
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-2"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-2", call_id="call-2", start_iso="2026-10-01T15:00:00")
    )
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-2"}})

    assert availability_stub == [], "the provider must never be reached by this probe"
    assert _outputs(sent)[1]["status"] == "duplicate_probe"
    assert len(_creates(sent)) == 1, "no further autonomous continuation for a repeated rejection"


# --------------------------------------------------------------------------- #
# Test 2 — legitimate continuation still works
# --------------------------------------------------------------------------- #
async def test_successful_probe_gets_an_unrestricted_continuation(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "Book HydroLux5 Face Only for October 1st at 3 PM.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-1"}})

    assert availability_stub == ["2026-10-01T15:00:00"]
    assert _outputs(sent)[0]["status"] == "available"
    creates = _creates(sent)
    assert len(creates) == 1
    assert "response" not in creates[0] or creates[0]["response"].get("tool_choice") != "none"


# --------------------------------------------------------------------------- #
# Test 3 — duplicate rejected probe within one turn is blocked
# --------------------------------------------------------------------------- #
async def test_duplicate_rejected_probe_in_one_turn_is_blocked_even_with_an_offset(
    voice_session, availability_stub, monkeypatch
):
    """The second attempt uses an explicit UTC offset for the same
    business-local instant — must still normalize to the same signature."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "Do you have anything?")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )
    assert _outputs(sent)[0]["status"] == "ungrounded_time"

    await voice_session._dispatch(
        _call_done_event(
            response_id="resp-1", call_id="call-2", start_iso="2026-10-01T15:00:00+00:00"
        )
    )
    assert availability_stub == []
    assert _outputs(sent)[1]["status"] == "duplicate_probe"


# --------------------------------------------------------------------------- #
# Test 4 — a genuinely new caller turn resets the block
# --------------------------------------------------------------------------- #
async def test_new_caller_turn_with_real_information_unblocks_the_previously_rejected_time(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "I'd like to book something.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )
    assert _outputs(sent)[0]["status"] == "ungrounded_time"
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-1"}})

    # The caller now actually states the date and time.
    await _caller_says(voice_session, "October 1st at 3 PM, please.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-2"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-2", call_id="call-2", start_iso="2026-10-01T15:00:00")
    )

    assert availability_stub == ["2026-10-01T15:00:00"]
    assert _outputs(sent)[1]["status"] == "available"


# --------------------------------------------------------------------------- #
# Test 5 — date established earlier, "3 PM" alone later: grounded only when
# the date was actually established
# --------------------------------------------------------------------------- #
async def test_date_established_earlier_then_time_only_turn_is_grounded(
    voice_session, availability_stub, monkeypatch
):
    """This is the exact reported scenario: the caller said "October first"
    earlier, then just "Uh, 3 P M." (STT's spelled-out rendering). Both
    pieces are real caller state, so the combination is valid."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "I'd like to book HydroLux5 Face Only for October 1st.")
    await _caller_says(voice_session, "Uh, 3 P M.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )

    assert _outputs(sent)[0]["status"] == "available"
    assert availability_stub == ["2026-10-01T15:00:00"]


async def test_time_only_turn_with_no_date_ever_established_is_rejected(
    voice_session, availability_stub, monkeypatch
):
    """Same "3 P M." turn, but no date was ever named — must not invent one."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "I'd like to book HydroLux5 Face Only.")
    await _caller_says(voice_session, "Uh, 3 P M.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )

    assert _outputs(sent)[0]["status"] == "ungrounded_time"
    assert availability_stub == []


# --------------------------------------------------------------------------- #
# Test 6 — earliest=true mismatched against an explicit "3 PM" ask
# --------------------------------------------------------------------------- #
async def test_earliest_after_explicit_time_request_is_rejected_without_looping(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "I want 3 PM today, specifically.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="", earliest=True)
    )
    assert availability_stub == []
    assert _outputs(sent)[0]["status"] == "ungrounded_earliest"
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-1"}})
    creates = _creates(sent)
    assert len(creates) == 1
    assert creates[0]["response"]["tool_choice"] == "none"

    # Retried in a new response with no new caller turn: blocked outright.
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-2"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-2", call_id="call-2", start_iso="", earliest=True)
    )
    assert availability_stub == []
    assert _outputs(sent)[1]["status"] == "duplicate_probe"
    assert len(_creates(sent)) == 1


# --------------------------------------------------------------------------- #
# Test 8 — tool result and response.done arriving close together produce
# exactly one continuation
# --------------------------------------------------------------------------- #
async def test_tool_result_and_response_done_race_yields_one_continuation(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "Book HydroLux5 Face Only for October 1st at 3 PM.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    # Tool result and response.done arrive back-to-back, as they would if
    # xAI closed the response immediately after emitting the function call.
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-1"}})

    assert len(_creates(sent)) == 1


# --------------------------------------------------------------------------- #
# Correctness must not depend on xAI actually honoring tool_choice: "none".
# These simulate the model ignoring it outright.
# --------------------------------------------------------------------------- #
async def test_model_ignoring_tool_choice_and_repeating_the_same_probe_is_still_blocked(
    voice_session, availability_stub, monkeypatch
):
    """Full 7-step scenario: probe rejected, restricted continuation issued,
    model calls the SAME tool again anyway (as if it ignored tool_choice),
    duplicate-signature protection blocks it, no further response.create is
    produced, exactly one real execution ever happens (zero, here, since
    the very first attempt is itself the rejected one), and the call stays
    alive (no exception) throughout."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "I'd like to book something.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-1"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-1", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )
    assert _outputs(sent)[0]["status"] == "ungrounded_time"
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-1"}})
    assert len(_creates(sent)) == 1  # the restricted continuation

    # The model ignores tool_choice: "none" and calls the tool again in the
    # SAME new response, with the SAME arguments.
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-2"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-2", call_id="call-2", start_iso="2026-10-01T15:00:00")
    )
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-2"}})

    assert availability_stub == [], "one real provider execution: zero, since nothing was ever grounded"
    assert _outputs(sent)[1]["status"] == "duplicate_probe"
    assert len(_creates(sent)) == 1, "no additional response.create for the blocked repeat"
    # The call is still usable: a genuinely new turn works normally.
    await _caller_says(voice_session, "October 1st at 3 PM, then.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-3"}})
    await voice_session._dispatch(
        _call_done_event(response_id="resp-3", call_id="call-3", start_iso="2026-10-01T15:00:00")
    )
    assert _outputs(sent)[2]["status"] == "available"


async def test_model_varying_arguments_each_time_is_eventually_stopped_by_the_chain_depth_cap(
    voice_session, availability_stub, monkeypatch
):
    """If the model ignores tool_choice AND varies its guess each time (so
    the duplicate-signature check never matches), the hard per-turn depth
    cap is the backstop that eventually terminates the chain."""
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "I'd like to book something.")
    hours = [10, 11, 12, 13, 14, 15, 16, 17]
    for i, hour in enumerate(hours):
        await voice_session._dispatch({"type": "response.created", "response": {"id": f"resp-{i}"}})
        await voice_session._dispatch(
            _call_done_event(
                response_id=f"resp-{i}", call_id=f"call-{i}",
                start_iso=f"2026-10-01T{hour:02d}:00:00",
            )
        )
        await voice_session._dispatch({"type": "response.done", "response": {"id": f"resp-{i}"}})

    assert availability_stub == [], "not one of these ever-changing guesses was grounded"
    statuses = [o["status"] for o in _outputs(sent)]
    assert "ungrounded_time" in statuses
    assert "tool_chain_limit" in statuses, "the depth cap must eventually trigger"
    # Once the depth cap has fired, no further continuation is issued for
    # this turn at all — verify the chain actually stopped, not just that
    # one message said so.
    limit_index = statuses.index("tool_chain_limit")
    assert all(s == "tool_chain_limit" for s in statuses[limit_index:])


# --------------------------------------------------------------------------- #
# response.create must be single-owner: one still-open response that saw
# BOTH a successful and a rejected tool result must not leave a stale,
# independently-tracked flag that fires again on some LATER, unrelated
# response.done.
# --------------------------------------------------------------------------- #
async def test_mixed_success_and_rejection_in_one_response_leaves_no_stale_continuation(
    voice_session, availability_stub, monkeypatch
):
    sent: list[dict] = []

    async def capture(payload):
        sent.append(payload)

    monkeypatch.setattr(voice_session, "_send", capture)

    await _caller_says(voice_session, "Book HydroLux5 Face Only for October 1st at 3 PM.")
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-mixed"}})
    # A successful probe first (would owe an unrestricted continuation)...
    await voice_session._dispatch(
        _call_done_event(response_id="resp-mixed", call_id="call-1", start_iso="2026-10-01T15:00:00")
    )
    # ...then a rejected one in the SAME still-open response (owes a
    # restricted continuation instead).
    await voice_session._dispatch(
        _call_done_event(response_id="resp-mixed", call_id="call-2", start_iso="2026-10-05T09:00:00")
    )
    assert _outputs(sent)[0]["status"] == "available"
    assert _outputs(sent)[1]["status"] == "ungrounded_time"

    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-mixed"}})
    creates = _creates(sent)
    assert len(creates) == 1
    assert creates[0]["response"]["tool_choice"] == "none", (
        "restricted must win when both a success and a rejection are pending"
    )

    # A completely unrelated LATER response, with no tool calls at all,
    # must not have its close trigger a leftover continuation.
    await voice_session._dispatch({"type": "response.created", "response": {"id": "resp-unrelated"}})
    await voice_session._dispatch({"type": "response.done", "response": {"id": "resp-unrelated"}})

    assert len(_creates(sent)) == 1, "no stale continuation fired on an unrelated response.done"
