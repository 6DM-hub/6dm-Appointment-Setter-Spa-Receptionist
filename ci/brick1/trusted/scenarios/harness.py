"""Independent verification harness for Brick 1 (SIMULATED - no real Square).

Drives the REAL realtime handler (XAIVoiceSession) turn by turn through its
dispatcher (_handle_function_call) and the real backend "caller said yes" path
(_confirm_pending_booking_from_caller), against the in-memory stateful
FakeSquare from tests/square_world.py (behind the real SquareAdapter).
Everything here is a simulation. Nothing talks to Square or production.
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.models import AppointmentStatus
from app.services.appointment_booking_service import BookingOutcome, confirm_booking, stage_booking
from app.services.booking_state import arm_verified_proposal, get_draft
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent
from tests.square_world import THU_2PM_UTC, call, local, voice_for, wire

CHI = ZoneInfo("America/Chicago")
HOUR = timedelta(hours=1)
T2 = THU_2PM_UTC
T3 = T2 + HOUR
T4 = T2 + 2 * HOUR
T5 = T2 + 3 * HOUR
Z2, Z3, Z4, Z5 = (t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in (T2, T3, T4, T5))
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=CHI)  # Tuesday before Thursday 22 Oct

_ids = itertools.count(1)


def make_world(monkeypatch):
    w = wire(monkeypatch)
    for start in (T2, T3, T4, T5):
        for team in ("TM_ANA", "TM_BEA"):
            w.square.open(start, "var_swedish_60", team)
            w.square.open(start, "var_facial_60", team)
    w.monkeypatch = monkeypatch
    return w


class Phone:
    """One simulated phone call through the real realtime handler."""

    def __init__(self, world, session: CallSession | None = None, voice=None):
        self.world = world
        self.session = session or call(world.spa)
        self.voice = voice or voice_for(world, self.session, world.monkeypatch)
        self.voice._now_provider = lambda tz: NOW.astimezone(tz)
        self.heard: list[str] = []          # verbatim force_message speech
        self.outputs: list[tuple[str, dict]] = []  # (tool, parsed output)
        self.voice._send = self._capture
        self._last_tool = None

        async def _no_consultation(*_a, **_k):
            return False  # the massage/facial consultation script is out of scope
        self.voice._enforce_service_consultation = _no_consultation

    async def _capture(self, payload):
        item = payload.get("item") or {}
        if item.get("type") == "force_message":
            self.heard.append(item["content"][0]["text"])
        elif item.get("type") == "function_call_output":
            try:
                out = json.loads(item["output"])
            except Exception:
                out = {"raw": item["output"]}
            self.outputs.append((self._last_tool, out))

    # ---- caller side --------------------------------------------------- #
    def turn(self, text: str) -> None:
        """A new caller turn arrives (transcript done)."""
        self.voice._pending_caller = text
        self.voice._flush_caller_turn()

    async def says(self, text: str, *, backend: bool = True) -> bool:
        """Caller speaks: the REAL transcript-completed event goes through the
        realtime dispatcher (_dispatch), which records the turn, captures a
        name answer, handles wrap-up, and lets the backend commit an explicit
        yes -- exactly as production does."""
        if not backend:
            self.turn(text)
            return False
        before = len(self.heard)
        await self.voice._dispatch({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": text,
        })
        return len(self.heard) > before

    # ---- model side ---------------------------------------------------- #
    async def tool(self, name: str, **args) -> dict:
        self._last_tool = name
        before = len(self.outputs)
        await self.voice._handle_function_call(
            {"name": name, "call_id": f"call_{next(_ids)}", "arguments": json.dumps(args)}
        )
        outs = self.outputs[before:]
        return outs[-1][1] if outs else {}

    async def tool_same_id(self, name: str, call_id: str, **args) -> dict:
        self._last_tool = name
        before = len(self.outputs)
        await self.voice._handle_function_call(
            {"name": name, "call_id": call_id, "arguments": json.dumps(args)}
        )
        outs = self.outputs[before:]
        return outs[-1][1] if outs else {}

    async def propose(self, **args) -> dict:
        return await self.tool("propose_appointment", **args)

    async def confirm(self) -> dict:
        return await self.tool("confirm_appointment")

    async def start_new(self) -> dict:
        return await self.tool("start_new_appointment")

    def last_heard(self) -> str:
        return self.heard[-1] if self.heard else ""

    # ---- inspection ---------------------------------------------------- #
    def square(self):
        out = []
        for b in self.world.square.active():
            seg = b["appointment_segments"][0]
            out.append({
                "start": b["start_at"], "variation": seg["service_variation_id"],
                "team": seg["team_member_id"], "customer": b.get("customer_id"),
                "note": b.get("customer_note"), "version": b["version"],
            })
        return sorted(out, key=lambda x: x["start"])

    def starts(self):
        return [b["start"] for b in self.square()]

    def rows(self):
        return sorted(self.world.store.live, key=lambda r: r.start_time)

    def json_roundtrip_ok(self) -> bool:
        json.dumps(self.session.to_dict())
        return True


async def book_2pm(world, *, realistic: bool = True) -> Phone:
    """Caller books Thursday 2 PM Swedish Massage end to end."""
    if realistic:
        p = Phone(world)
        p.turn("Hi, I'd like a Swedish massage on Thursday at 2 PM. My name is Ada Lovelace.")
        out = await p.propose(requested_start_iso=local(T2), service_description="Swedish Massage",
                              caller_name="Ada Lovelace", operation="schedule")
        assert out.get("status") == "draft", (out, p.heard, p.outputs)
        await p.says("Yes, please.")
        assert p.starts() == [Z2], (p.heard, p.outputs, p.square())
        assert p.voice._booking_completed_turn is not None, p.heard
        return p
    session = call(world.spa)
    staged = await stage_booking(world.db, session, AppointmentIntent(
        intent="schedule", confidence=1.0, caller_name="Ada Lovelace",
        requested_start_iso=local(T2), service_description="Swedish Massage"))
    assert staged.outcome is BookingOutcome.DRAFT, staged.message
    arm_verified_proposal(session)
    booked = await confirm_booking(world.db, session)
    assert booked.outcome is BookingOutcome.BOOKED
    session.booking_status = "booked"
    p = Phone(world, session)
    p.voice._user_turn_count = 3
    p.voice._booking_completed_turn = 3
    return p


async def engine_yes(p: Phone):
    """Worst case: caller's 'yes' reaches confirm_booking for whatever the
    draft currently holds (bypassing realtime gates)."""
    arm_verified_proposal(p.session)
    return await confirm_booking(p.world.db, p.session)
