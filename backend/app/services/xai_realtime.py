"""
Drives one xAI realtime voice session for an inbound SIP call.

Inbound calls on a number attached to a Twilio Elastic SIP trunk pointed at
sip.voice.x.ai never reach the <Gather> webhooks in app/api/v1/telephony.py —
the trunk overrides the number's voice_url. xAI instead POSTs a
`realtime.call.incoming` webhook, and the call only becomes a conversation once
something connects to the realtime WebSocket for that call_id and configures it.

This module is that something. Per call it:
  1. opens wss://api.x.ai/v1/realtime?call_id=...
  2. sends `session.update` with the tenant's persona, voice and tool schema
  3. speaks the greeting immediately, so the caller is not met with silence
  4. services `manage_appointment` tool calls through the existing booking engine
  5. accumulates both sides of the conversation and persists the transcript,
     summary and status onto the same CallLog row the <Gather> flow writes

Event-name coverage is deliberately tolerant. The wire contract here is taken
from xAI's published SIP/Voice Agent docs rather than from live traffic, so each
handler accepts the documented name plus near-neighbour spellings, and anything
unrecognised is logged at debug under `_UNHANDLED` instead of being dropped
silently. Tighten this once real sessions have been observed.
"""
import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import websockets
from sqlalchemy import select
from websockets.asyncio.client import connect as ws_connect

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis import redis_manager
from app.models import CallDirection, CallLog, CallStatus
from app.services.appointment_booking_service import (
    BookingOutcome,
    apply_booking_result,
    attempt_booking,
    cancel_booking,
    check_availability_only,
    confirm_booking,
    search_day_part,
    stage_booking,
    lookup_upcoming_appointments,
    _parse_dt,
    _prepare,
)
from app.services.spa_facts import lookup_spa_facts
from app.services.booking_state import (
    CALLER_NAME_QUESTION,
    CARD_ON_FILE_HESITANT,
    CARD_ON_FILE_POLICY,
    authoritative_availability_speech,
    booking_card_speech,
    caller_name_required_for,
    capture_caller_name_answer,
    contains_unauthorized_availability_claim,
    get_draft,
    grounded_availability_speech,
    is_affirmative,
    looks_card_hesitant,
    looks_like_unverified_booking_success,
    mark_read_back,
    record_pure_confirmation,
    recover_caller_name_from_history,
    save_draft,
    start_new_intent,
    supplied_caller_name,
    utterance_modifies_booking,
    _parse_provider_iso,
)
from app.services.business_hours import resolve_timezone
from app.services.call_state import (
    CALL_PHASE_GREETING_REQUESTED,
    CALL_PHASE_NEW,
    CallSession,
    CallStateStore,
    mark_call_ended,
    mark_conversation_active,
    mark_greeting_requested,
    mark_greeting_sent,
)
from app.services.caller_identity import persist_caller_identity
from app.services.grok_service import (
    AppointmentIntent,
    build_realtime_instructions,
    grok_service,
    primary_caller_language,
)
from app.services.truth_log import truth
from app.services.voice_config import resolve_xai_voice

logger = logging.getLogger(__name__)

HOLD_ACK_TEXT = "Let me check that for you."
AVAILABILITY_CACHE_SECONDS = 3

_SESSION_RESTART_GREETING_RE = re.compile(
    r"^\s*(?:hi|hello|hey)[,!.]?\s+(?:thank you|thanks) for calling\b|"
    r"^\s*(?:thank you|thanks) for calling\b",
    re.IGNORECASE,
)
_GREET_THEM_PROMPT = "[The caller has just connected. Greet them.]"

# A draft is considered genuinely read back only when the assistant explicitly
# asks the caller to approve the pending appointment.  The backend already
# verified the slot before booking_status becomes awaiting_confirmation; this
# cue prevents a stray "yes" to an unrelated question from committing it.
_CONFIRMATION_QUESTION_RE = re.compile(
    r"(?:"
    r"\b(?:does|would) that (?:work|be okay)\b|"
    r"\bdoes that sound good\b|"
    r"\bis that (?:okay|correct|right)\b|"
    r"\b(?:shall|should|can|may) i (?:go ahead and )?(?:book|confirm|schedule)\b|"
    r"\bwould you like me to (?:book|confirm|schedule)\b|"
    r"\bdo you want me to (?:book|confirm|schedule)\b|"
    r"\bwould you like to (?:confirm|proceed|book it)\b|"
    r"\bcan you confirm\b"
    r")",
    re.IGNORECASE,
)

# Detect caller-stated times so an exact availability check is grounded
# in something the caller actually said.
_TIME_WORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
)

# AM/PM as the transcript actually renders it. Speech-to-text frequently
# spells this out letter by letter ("3 P M", "P. M."), so the letters
# themselves may have whitespace/punctuation between them, not just between
# the number and the letters.
_MERIDIEM = r"[ap]\.?\s*m\.?"

_TIME_MENTION_RE = re.compile(
    # Numeric time with AM/PM:
    # "3pm", "3 PM", "3 P M", "3:30 PM", "03:00 p.m."
    r"\b(?:0?[1-9]|1[0-2])(?::[0-5]\d)?\s*"
    rf"{_MERIDIEM}(?=\s|$|[,!?])|"

    # Spoken-number time with AM/PM:
    # "three PM", "ten a.m.", "three P M"
    rf"\b{_TIME_WORD}\s*"
    rf"{_MERIDIEM}(?=\s|$|[,!?])|"

    # Conversational numeric time:
    # "at 3", "around 3:30", "by 10"
    r"\b(?:at|around|by)\s+"
    r"(?:0?[1-9]|1[0-2])(?::[0-5]\d)?\b|"

    # Conversational spoken time:
    # "at three", "around ten", "at three o'clock"
    rf"\b(?:at|around|by)\s+"
    rf"{_TIME_WORD}(?:\s*o.?clock)?\b|"

    # Explicit natural times
    r"\bnoon\b|\bmidnight\b|"
    r"\bo.?clock\b|"

    # 24-hour clock:
    # "14:00", "09:30"
    r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b",

    re.IGNORECASE,
)

_EARLIEST_REQUEST_RE = re.compile(
    r"\b(earliest|soonest|next\s+(?:available|opening)|"
    r"first\s+(?:available|opening)|"
    r"as\s+soon\s+as\s+possible|asap)\b",
    re.IGNORECASE,
)

_RESCHEDULE_REQUEST_RE = re.compile(
    r"\b(reschedule|move|change)\b.{0,40}\b(appointment|booking|it|that)\b|"
    r"\b(appointment|booking)\b.{0,40}\b(reschedule|move|change)\b",
    re.IGNORECASE,
)

# A caller turn naming a DAY, as opposed to an hour. "3pm" alone establishes
# no date; combining it with a date the caller never actually named would be
# inventing one. Deliberately excludes "today"/"tonight" — treated as
# needing exactly as explicit a mention as any other day, not a free pass.
_DATE_MENTION_RE = re.compile(
    r"\btomorrow\b|"
    r"\b(mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)(day)?\b|"
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b|"
    r"\b\d{1,2}(st|nd|rd|th)\b|"
    r"\b\d{1,2}[/-]\d{1,2}\b|"
    r"\bnext\s+week\b|\bthis\s+week\b|\btoday\b|\btonight\b",
    re.IGNORECASE,
)

# The caller explicitly dropping/reversing a date they just named — e.g.
# "forget that", "never mind that date", "not that day". Seeing one of these
# with no date in the same breath means whatever date came before it is no
# longer active, and must not be silently resurrected for a later
# time-only turn.
_WRAP_UP_RE = re.compile(
    r"^\s*(no|nope|no thanks|no thank you|that'?s all|that'?s it|nothing else|"
    r"i'?m good|all set|we'?re good)\b",
    re.IGNORECASE,
)


def _caller_is_finished(utterance: str) -> bool:
    """A bare no after the post-booking question, not a new request."""
    text = (utterance or "").strip()
    if not text or not _WRAP_UP_RE.search(text):
        return False
    return re.search(
        r"\b(book|appointment|reschedule|cancel|tomorrow|monday|tuesday|wednesday|"
        r"thursday|friday|saturday|sunday|am|pm|morning|afternoon|evening)\b",
        text,
        re.IGNORECASE,
    ) is None


_DATE_CANCEL_RE = re.compile(
    r"\bforget (that|it)\b|\bnever\s*mind\b|\bnot that date\b|\bnot that day\b|"
    r"\bscratch that\b|\bcancel that\b|\bdifferent day\b",
    re.IGNORECASE,
)

# Explicit, narrow date extraction — deliberately NOT a general-purpose fuzzy
# parser. A quick check found `dateutil.parser.parse(..., fuzzy=True)` will
# silently return a wrong-but-valid-looking date for exactly this kind of
# input (e.g. "anything free tomorrow around 10?" parsed as day=10 of the
# CURRENT month, ignoring "tomorrow" entirely) — worse than refusing, since
# it fabricates false confidence instead of asking the caller. Only the
# specific forms below are trusted; anything else resolves to None and the
# probe is treated as ungrounded rather than guessed.
_MONTH_NUMBERS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH_NAME_GROUP = "|".join(_MONTH_NUMBERS)
_MONTH_THEN_DAY_RE = re.compile(
    rf"\b({_MONTH_NAME_GROUP})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE
)
_DAY_THEN_MONTH_RE = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_NAME_GROUP})\b", re.IGNORECASE
)
_NUMERIC_MONTH_DAY_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})\b")


def _extract_explicit_date(utterance: str, today: date) -> date | None:
    """A calendar date this utterance names with enough confidence to act
    on — "October 1st", "1st of October", "10/1", "tomorrow", "today"/
    "tonight", or a weekday such as "Thursday". A year is never stated on
    a phone call, so the nearest upcoming occurrence is assumed. "Next
    week" with no weekday still returns None.
    """
    month = day = None
    match = _MONTH_THEN_DAY_RE.search(utterance)
    if match:
        month, day = _MONTH_NUMBERS[match.group(1).lower()], int(match.group(2))
    else:
        match = _DAY_THEN_MONTH_RE.search(utterance)
        if match:
            day, month = int(match.group(1)), _MONTH_NUMBERS[match.group(2).lower()]
        else:
            match = _NUMERIC_MONTH_DAY_RE.search(utterance)
            if match:
                month, day = int(match.group(1)), int(match.group(2))

    if month is not None and day is not None:
        try:
            candidate = date(today.year, month, day)
        except ValueError:
            return None
        if candidate < today:
            try:
                candidate = date(today.year + 1, month, day)
            except ValueError:
                return None
        return candidate

    lowered = utterance.lower()
    if re.search(r"\btomorrow\b", lowered):
        return today + timedelta(days=1)
    if re.search(r"\btoday\b|\btonight\b", lowered):
        return today
    return _extract_weekday_date(utterance, today)


_WEEKDAY_INDEX = {
    "mon": 0, "monday": 0,
    "tue": 1, "tues": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}
_WEEKDAY_RE = re.compile(
    r"\b(?:(?P<next_week>next\s+week)\s+)?"
    r"(?:(?P<rel>this|coming|next)\s+)?"
    r"(?P<day>monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)\b",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(
    rf"\b(?P<h>[01]?\d|2[0-3]|{_TIME_WORD})"
    rf"(?::(?P<m>[0-5]\d))?\s*(?P<ap>{_MERIDIEM})\b",
    re.IGNORECASE,
)
_DAY_PART_HOURS = {
    "morning": (dt_time(9, 0), dt_time(12, 0)),
    "afternoon": (dt_time(12, 0), dt_time(17, 0)),
    "evening": (dt_time(17, 0), dt_time(21, 0)),
}
_DAY_PART_RE = re.compile(r"\b(morning|afternoon|evening)\b", re.IGNORECASE)
_SPOKEN_HOUR = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}


def _extract_weekday_date(utterance: str, today: date) -> date | None:
    """The next upcoming occurrence of a named weekday.

    "Saturday", "this Saturday", and "coming Saturday" are the soonest
    Saturday, including today. "Next Saturday" said on a Saturday means a
    week from today. "Next week Saturday" is that weekday in the following
    week. A bare weekday is not a guess: the business's current local date
    fixes it.
    """
    match = _WEEKDAY_RE.search(utterance or "")
    if match is None:
        return None
    weekday = _WEEKDAY_INDEX[match.group("day").lower()]
    days = (weekday - today.weekday()) % 7
    if match.group("rel") and match.group("rel").lower() == "next" and days == 0:
        days = 7
    if match.group("next_week"):
        days += 7
    return today + timedelta(days=days)


def _parse_hour_token(token: str) -> int | None:
    word = token.lower()
    if word in _SPOKEN_HOUR:
        return _SPOKEN_HOUR[word]
    try:
        return int(word)
    except ValueError:
        return None


def _extract_clock_time(utterance: str) -> dt_time | None:
    """A clock time the caller actually said. No AM/PM means no clock time."""
    text = utterance or ""
    if re.search(r"\b(?:noon|midday)\b", text, re.IGNORECASE):
        return dt_time(12, 0)
    if re.search(r"\bmidnight\b", text, re.IGNORECASE):
        return dt_time(0, 0)
    match = None
    for found in _CLOCK_RE.finditer(text):
        match = found
    if match is None:
        return None
    hour = _parse_hour_token(match.group("h"))
    if hour is None or hour > 23:
        return None
    minute = int(match.group("m") or 0)
    meridiem = re.sub(r"[^apm]", "", (match.group("ap") or "").lower())
    if meridiem.startswith("p") and hour < 12:
        hour += 12
    elif meridiem.startswith("a") and hour == 12:
        hour = 0
    elif meridiem.startswith("a") and hour > 12:
        return None
    try:
        return dt_time(hour, minute)
    except ValueError:
        return None


def _extract_day_part(utterance: str) -> tuple[dt_time, dt_time] | None:
    """Morning, afternoon, or evening when the caller did not name an hour."""
    if _extract_clock_time(utterance):
        return None
    match = _DAY_PART_RE.search(utterance or "")
    if match is None:
        return None
    return _DAY_PART_HOURS[match.group(1).lower()]

# How many of the most recent caller turns count as "still establishing the
# date" for a later turn that names only a time. Kept short deliberately: a
# date mentioned many turns ago in an unrelated part of the conversation is
# not the caller confirming that date now.
_RECENT_DATE_CONTEXT_TURNS = 4
# Prefix distinguishing an xAI voice call from a Twilio CallSid in the shared
# `call_logs.twilio_call_sid` column. 4 + 36 chars fits the String(64) column,
# so this needs no migration.
XAI_SID_PREFIX = "xai:"
XAI_REALTIME_MODEL = "grok-voice-latest"


def build_xai_realtime_url(*, call_id: str | None = None) -> str:
    """Build a realtime URL that always pins the current recommended voice model.

    `XAI_REALTIME_URL` may already contain query parameters, so preserve them
    instead of blindly appending another `?`.  Explicit model selection avoids
    drifting onto whatever server default happens to be active.
    """
    parts = urlsplit(settings.XAI_REALTIME_URL)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["model"] = query.get("model") or XAI_REALTIME_MODEL
    if call_id is not None:
        query["call_id"] = call_id
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def xai_call_sid(call_id: str) -> str:
    return f"{XAI_SID_PREFIX}{call_id}"


PROPOSE_APPOINTMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "propose_appointment",
    "description": (
        "Record what the caller is asking for and check whether the slot is free. "
        "This NEVER books anything. Call it every time the caller names or changes "
        "a date, time or service — including when they change their mind. There is "
        "only one pending request per call, so a change replaces the previous one "
        "rather than adding a second appointment. After calling this, the backend "
        "speaks the Square result. If caller_name is not already stored from this "
        "call, the backend asks for the caller's name before any booking "
        "confirmation. A known profile or a Square customer matched by phone is "
        "not that name. Pass caller_name once the caller says it, and do not ask "
        "again when it is already stored.\n\n"
        "NEVER invent or guess a date/time to 'try' it. A weekday is the next "
        "upcoming one — do not ask if they mean the coming Saturday. "
        "'Thursday at 4pm' is complete: set requested_start_iso to that local "
        "time. 'Saturday afternoon' is also complete: call this tool; the "
        "backend searches that part of the day. If the caller has not stated "
        "a day or a time at all, either ask them, or — if they asked for the "
        "earliest/soonest/next opening — set earliest=true and leave "
        "requested_start_iso unset; the backend runs one real forward search "
        "and returns up to three actual openings. Calling this again with a "
        "different self-chosen time because the last one didn't work is not "
        "allowed and will be refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "caller_name": {"type": "string", "description": "Caller's full name."},
            "caller_email": {"type": "string", "description": "Caller's email, if given."},
            "requested_start_iso": {
                "type": "string",
                "description": (
                    "Requested start as THIS BUSINESS'S LOCAL wall-clock time in "
                    "ISO8601 with NO timezone suffix and NO 'Z' — e.g. "
                    "2026-09-04T14:00:00 for 2pm local time. Never convert this to "
                    "UTC yourself and never append 'Z'; the backend localizes it. "
                    "ONLY set this to a time the caller actually said, or one of the "
                    "authoritative options already offered to them. Leave unset "
                    "when earliest=true."
                ),
            },
            "requested_end_iso": {
                "type": "string",
                "description": (
                    "Requested end as THIS BUSINESS'S LOCAL wall-clock time, same "
                    "format as requested_start_iso (no 'Z', no offset). Omit to use "
                    "the service's duration."
                ),
            },
            "service_description": {
                "type": "string",
                "description": "Service requested, e.g. '60 minute deep tissue massage'.",
            },
            "preferred_staff": {
                "type": "string",
                "description": (
                    "Name of the specific staff member the caller asked for, e.g. "
                    "'Sarah'. Leave this out entirely if the caller said 'anyone', "
                    "'whoever's free', or didn't mention a preference — never guess "
                    "a name."
                ),
            },
            "operation": {
                "type": "string",
                "enum": ["schedule", "reschedule"],
                "description": (
                    "Use reschedule when the caller is moving an existing appointment; "
                    "otherwise use schedule. A reschedule never creates a second appointment."
                ),
            },
            "appointment_id": {
                "type": "string",
                "description": (
                    "For reschedule only: an appointment_id previously returned by the "
                    "backend lookup/offered choices. Never invent an ID."
                ),
            },
            "earliest": {
                "type": "boolean",
                "description": (
                    "True ONLY when the caller asked for the earliest/soonest/next "
                    "available opening and named no specific date or time "
                    "themselves. When true, leave requested_start_iso unset — the "
                    "backend performs the actual forward search."
                ),
            },
            "guest_name": {
                "type": "string",
                "description": (
                    "Name of the person the appointment is FOR when the caller is "
                    "booking for someone else. Leave empty if the appointment is "
                    "for the caller. Never copy preferred_staff here."
                ),
            },
        },
        "required": [],
    },
}

CONFIRM_APPOINTMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "confirm_appointment",
    "description": (
        "Actually book the pending request, after the caller has explicitly agreed "
        "to the exact date and time you read back to them. Takes no date: it always "
        "commits the latest pending request, so it cannot book a time the caller has "
        "already changed. Safe to call more than once — repeat calls return the same "
        "appointment instead of creating another. If the caller changed their mind "
        "after a booking was made, this moves that booking rather than adding one. "
        "IMPORTANT: after the caller says yes/book it/that works, call this tool immediately "
        "BEFORE speaking any acknowledgment or success wording. Do not call this until "
        "caller_name is stored. The backend speaks the final confirmation only after "
        "the provider returns a real external booking ID, and it mentions a secure "
        "card text only when that text was actually sent."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

LOOKUP_APPOINTMENTS_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "lookup_appointments",
    "description": (
        "Read-only lookup of this caller's upcoming appointments. Use this for questions "
        "like 'when is my next appointment?' and before selecting among multiple appointments "
        "for cancellation or rescheduling. This never books, moves, or cancels anything."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "purpose": {
                "type": "string",
                "enum": ["lookup", "cancel", "reschedule"],
                "description": "Why the appointments are being listed.",
            }
        },
        "required": [],
    },
}

CANCEL_APPOINTMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "cancel_appointment",
    "description": (
        "Cancel this caller's appointment. If several upcoming appointments exist, first "
        "read back the backend-provided choices and then pass the appointment_id for the "
        "one the caller selected. Never invent an appointment_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "appointment_id": {
                "type": "string",
                "description": "One of the appointment IDs previously offered by the backend.",
            }
        },
        "required": [],
    },
}

NEW_APPOINTMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "start_new_appointment",
    "description": (
        "Start a SECOND, separate appointment. Only call this when the caller "
        "explicitly says they want an additional appointment as well as the one "
        "already arranged. Never call it because they changed the date or time of "
        "the appointment already being discussed — use propose_appointment for that."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

MANAGE_APPOINTMENT_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "manage_appointment",
    "description": (
        "Commit a booking, reschedule or cancellation the caller has explicitly "
        "agreed to. Prefer propose_appointment followed by confirm_appointment; "
        "this remains for a single-step confirmed action. Repeat calls resolve to "
        "the same appointment rather than creating another."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "enum": ["schedule", "reschedule", "cancel"],
                "description": "What the caller wants to do.",
            },
            "caller_name": {"type": "string", "description": "Caller's full name."},
            "caller_email": {"type": "string", "description": "Caller's email, if given."},
            "requested_start_iso": {
                "type": "string",
                "description": (
                    "Requested start as THIS BUSINESS'S LOCAL wall-clock time in "
                    "ISO8601 with NO timezone suffix and NO 'Z' — e.g. "
                    "2026-09-04T14:00:00 for 2pm local time. Never convert this to "
                    "UTC yourself and never append 'Z'; the backend localizes it."
                ),
            },
            "requested_end_iso": {
                "type": "string",
                "description": (
                    "Requested end as THIS BUSINESS'S LOCAL wall-clock time, same "
                    "format as requested_start_iso (no 'Z', no offset). Omit to use "
                    "the service's duration."
                ),
            },
            "service_description": {
                "type": "string",
                "description": "Service requested, e.g. '60 minute deep tissue massage'.",
            },
            "preferred_staff": {
                "type": "string",
                "description": (
                    "Name of the specific staff member the caller asked for. Leave "
                    "this out if they have no preference."
                ),
            },
        },
        "required": ["intent"],
    },
}

CHECK_AVAILABILITY_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "check_availability",
    "description": (
        "Check one EXACT spa slot the caller actually stated, or one of the "
        "options already offered to them. This never books or reserves it. "
        "NEVER call this with a date/time you invented yourself to see if it "
        "happens to be free — that is not allowed and will be refused. For "
        "'earliest/soonest opening' requests use propose_appointment with "
        "earliest=true instead; it runs the real forward search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "requested_start_iso": {
                "type": "string",
                "description": (
                    "This business's LOCAL wall-clock time, ISO8601, no 'Z', no "
                    "offset — e.g. 2026-09-04T14:00:00 for 2pm local time. Must be "
                    "a time the caller said, or one of the options already offered."
                ),
            },
            "requested_end_iso": {
                "type": "string",
                "description": "Same local, no-offset format as requested_start_iso.",
            },
            "service_description": {"type": "string"},
            "preferred_staff": {
                "type": "string",
                "description": "Specific staff member requested, if any.",
            },
        },
        "required": ["requested_start_iso"],
    },
}

LOOKUP_SPA_FACTS_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "lookup_spa_facts",
    "description": (
        "Look up authoritative spa business facts from the dashboard and the "
        "active booking provider. REQUIRED before answering questions about "
        "location, address, hours, phone, services, prices, policies, packages, "
        "upsells, or payment. Never invent a fact if this tool returns unknown."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "enum": [
                    "location",
                    "hours",
                    "phone",
                    "services",
                    "prices",
                    "policies",
                    "packages",
                    "vip",
                    "upsells",
                    "payment",
                    "all",
                ],
            },
            "vip_identifier": {
                "type": "string",
                "description": "VIP package name or identifier the caller claims, if any.",
            },
            "service_name": {
                "type": "string",
                "description": "Service to look up for prices or upsells, if named.",
            },
            "location_query": {
                "type": "string",
                "description": "Branch or city the caller named, if any.",
            },
        },
        "required": ["topic"],
    },
}

# The set offered to the model. Splitting "what the caller wants" from "write it
# down" is the structural half of the duplicate-booking fix: the only tool that
# writes takes no date, so it can only ever commit the single pending request,
# and the tool that carries a date can only ever overwrite that request.
REQUEST_CALLBACK_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "request_callback",
    "description": (
        "Store a customer callback request. This is not a staff notification and "
        "must never include a card number, CVV, or other payment digits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string"},
            "preferred_window": {
                "type": "string",
                "description": "When the caller asked to be called back, in their words.",
            },
            "caller_name": {"type": "string"},
        },
        "required": ["reason"],
    },
}

VOICE_TOOLS: list[dict[str, Any]] = [
    LOOKUP_SPA_FACTS_TOOL,
    CHECK_AVAILABILITY_TOOL,
    PROPOSE_APPOINTMENT_TOOL,
    CONFIRM_APPOINTMENT_TOOL,
    LOOKUP_APPOINTMENTS_TOOL,
    CANCEL_APPOINTMENT_TOOL,
    NEW_APPOINTMENT_TOOL,
    REQUEST_CALLBACK_TOOL,
]

# Caller-speech transcription. The documented event is the `.updated` form,
# which carries a *cumulative* transcript for the current turn.
_CALLER_TRANSCRIPT_UPDATED = (
    "conversation.item.input_audio_transcription.updated",
    "conversation.item.input_audio_transcription.delta",
)
_CALLER_TRANSCRIPT_DONE = (
    "conversation.item.input_audio_transcription.completed",
    "conversation.item.input_audio_transcription.done",
)
_SPEECH_STARTED_EVENTS = (
    "input_audio_buffer.speech_started",
    "input_audio_buffer.speech_start",
)
_AUDIO_DELTA_EVENTS = (
    "response.output_audio.delta",
    "response.audio.delta",
)
# The agent's own words, as text alongside the synthesised audio. Observed on a
# live session: these arrive as incremental `.delta` events and the run is ended
# by `response.output_audio.done` — no transcript `.done` is emitted at all, so
# the deltas must be accumulated or the agent's half of every call is lost.
_AGENT_TRANSCRIPT_DELTA = (
    "response.output_audio_transcript.delta",
    "response.audio_transcript.delta",
    "response.output_text.delta",
)
_AGENT_TRANSCRIPT_DONE = (
    "response.output_audio_transcript.done",
    "response.audio_transcript.done",
    "response.output_text.done",
    "response.output_audio.done",
    "response.content_part.done",
)
# Protocol chatter with no bearing on the transcript. Listed explicitly so the
# _UNHANDLED log stays a genuine signal of events worth handling.
_BENIGN_EVENTS = frozenset({
    "session.created",
    "session.updated",
    "conversation.created",
    "conversation.item.added",
    "conversation.item.created",
    "ping",
    "response.created",
    "response.done",
    "response.output_item.added",
    "response.output_item.done",
    "response.content_part.added",
    "input_audio_buffer.committed",
    "input_audio_buffer.speech_stopped",
    "output_audio_buffer.started",
    "output_audio_buffer.stopped",
    "rate_limits.updated",
    # We reconstruct nothing from the incremental pieces: xAI has always been
    # observed to deliver the complete, valid-JSON arguments string in a
    # single `.done` event, which is the only one we act on. Ignoring these
    # deltas is intentional, not an oversight — listed here (instead of
    # falling through to `_UNHANDLED`) so that log line stays a genuine
    # signal that something new needs a handler.
    "response.function_call_arguments.delta",
})

_FUNCTION_CALL_DONE = (
    "response.function_call_arguments.done",
    "response.function_call.done",
)


def cached_availability_output(
    recent: dict[str, tuple[float, str]],
    signature: str,
    now: float,
    *,
    source: str,
) -> str | None:
    """Reuse a just-finished identical lookup. Booking revalidation never does."""
    if source == "revalidation":
        return None
    hit = recent.get(signature)
    if hit is None or now - hit[0] >= AVAILABILITY_CACHE_SECONDS:
        return None
    return hit[1]


def _first_str(payload: Any, *keys: str) -> str | None:
    """First non-empty string at any of `keys` in a possibly-nested payload."""
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for value in payload.values():
        if isinstance(value, dict):
            found = _first_str(value, *keys)
            if found:
                return found
    return None


class XAIVoiceSession:
    """One realtime voice call: WebSocket in, CallLog row out."""

    #: Within one response, how many availability-probing tool calls
    #: (check_availability / propose_appointment) are allowed before further
    #: ones are refused instead of reaching Square. The live failure this
    #: guards: the model fabricating a sequence of candidate times and
    #: calling the tool once per guess instead of asking the caller or using
    #: a real backend search.
    MAX_AVAILABILITY_PROBES_PER_RESPONSE = 1

    #: Hard ceiling on tool round-trips (of any kind) within one caller turn.
    #: High enough for a legitimate multi-step exchange (e.g. propose ->
    #: confirm), low enough that a rejection loop cannot run away even if
    #: every other guard were somehow bypassed. Resets on each new turn.
    MAX_TOOL_CHAIN_DEPTH_PER_TURN = 6
    HOLD_ACK_DELAY_SECONDS = 1.5
    HOLD_TONE_DELAY_SECONDS = 1.2
    GREETING_WATCHDOG_SECONDS = 2.5

    def __init__(self, call_id: str, session: CallSession) -> None:
        self.call_id = call_id
        self.session = session
        self._ws: Any = None
        self._pending_caller = ""
        self._pending_agent = ""
        self._greeted = bool(session.greeting_sent)
        self._greeting_pending = bool(session.greeting_requested and not session.greeting_sent)
        self._greeting_request_inflight = False
        self._greeting_response_id: str | None = None
        self._greeting_got_audio = bool(session.greeting_audio_started)
        self._greeting_fallback_used = False
        self._greeting_watchdog_task: asyncio.Task | None = None
        self._xai_session_ready = asyncio.Event()
        self._turn_started_at: float | None = None
        self._turn_timings: dict[str, float] = {}
        self._first_xai_audio_logged = False
        self._first_caller_audio_logged = False
        self._deadline = time.monotonic() + settings.XAI_VOICE_MAX_CALL_SECONDS
        # Resolved once per call for the availability-claim guard below.
        self._tz = resolve_timezone(session.timezone)
        # Response lifecycle tracking. xAI can emit several function calls
        # under one `response_id` before that response is done; without
        # this, nothing distinguishes "a response is still open" from "the
        # last one already finished", which is what let a stray
        # `response.cancel` fire with nothing active, and let a stale
        # (already-cancelled) response's function call still run.
        self._active_response_id: str | None = None
        self._cancelled_response_ids: set[str] = set()
        self._handled_call_ids: set[str] = set()
        self._availability_probes_by_response: dict[str, int] = {}
        # Cached function_call_output per call_id, so a duplicate delivery
        # of the same call (a protocol-level retry) can be answered again
        # without re-running the underlying tool.
        self._call_id_outputs: dict[str, str] = {}
        # When a tool finishes while the response that requested it is still
        # open, xAI cannot accept another response.create yet. Remember that
        # the tool result still needs a follow-up response and resume as soon
        # as response.done closes the current response. Without this flag the
        # model stays silent until the caller speaks again.
        self._response_needed_after_tool = False
        # Some tool outcomes (most importantly a successful booking) should not
        # be handed back to the model as an unconstrained fresh turn. The live
        # call showed Grok occasionally restarting with a receptionist greeting
        # after a successful booking instead of confirming the appointment.
        # Store one deterministic force_message here and emit it only after the
        # response that requested the tool has fully closed.
        self._pending_forced_tool_message: str | None = None
        self._awaiting_wrap_up = False
        self._deferred_wrap_up = False
        self._blocked_unverified_success = False
        # Turn-scoped temporal grounding. Unlike the per-response cap above,
        # this survives across `response_id`s for the whole call: a new
        # response must not, by itself, re-grant permission to probe an
        # exact time the caller never actually stated in a NEW turn.
        self._user_turn_count = 0
        self._last_probe_turn_count = 0
        # Exact timestamps that have already passed temporal grounding for the
        # caller's CURRENT stated time. This lets a provider/tool retry reuse
        # the same caller-approved instant without requiring the caller to say
        # the time again, while still blocking a different model-invented time.
        #
        # The set is cleared when the caller states a new time of day.
        self._grounded_exact_times: set[str] = set()
        # Guard-rail rejections (ungrounded time/earliest, per-response cap)
        # must not autonomously re-trigger the same probe forever. These two
        # are the loop-breaker: an identical rejected probe signature is
        # refused outright on retry, and a hard depth cap bounds how many
        # tool round-trips one caller turn can spend regardless. Both reset
        # the moment a genuinely new caller turn arrives.
        self._rejected_probe_signatures_this_turn: set[tuple[str, str | bool]] = set()
        self._tool_chain_depth_this_turn = 0
        # Set instead of `_response_needed_after_tool` when the deferred
        # continuation must not be allowed to call a tool again — a rejected
        # probe's follow-up may only ask the caller something, never retry.
        self._restricted_response_needed_after_tool = False
        self._inflight_tools: set[asyncio.Task] = set()
        self._hold_ack_played_this_turn = False
        self._availability_recent: dict[str, tuple[float, str]] = {}
        self._availability_lookup_open = False
        self._muted_availability_response_id: str | None = None
        self._availability_model_response_id: str | None = None
        self._availability_hold_response_id: str | None = None
        self._availability_hold_done = False
        self._availability_expect_hold = False
        self._pending_availability_speech: str | None = None
        self._prompt_caller_name_after_response = False
        self._caller_name_prompt_after_id: str | None = None
        self._caller_name_question_sent = False
        self._caller_name_reask_turn: int | None = None
        self._after_availability_line: str | None = None
        self._availability_speech_interrupted = False
        self._recover_availability_tool = False

    # ---------------------------------------------------------------- plumbing
    @property
    def _url(self) -> str:
        return build_xai_realtime_url(call_id=self.call_id)

    async def _send(self, payload: dict[str, Any]) -> None:
        await self._ws.send(json.dumps(payload))

    @staticmethod
    def _response_id_of(data: dict[str, Any]) -> str | None:
        """The response id an event belongs to, however the event shapes it:
        a flat `response_id` (function-call events) or a nested
        `response.id` (`response.created`/`response.done`)."""
        flat = data.get("response_id")
        if isinstance(flat, str) and flat:
            return flat
        nested = data.get("response")
        if isinstance(nested, dict):
            nested_id = nested.get("id")
            if isinstance(nested_id, str) and nested_id:
                return nested_id
        return None

    async def _cancel_active_response(self) -> None:
        """Send `response.cancel` only when a response is actually active.

        Unconditionally sending this (the previous behaviour, on every
        barge-in and on both booking/availability guards) is what produced
        "Cancellation failed: no active response found" on a live call —
        harmless on its own, but it also means a cancel could be sent for a
        response that had already finished, right as a new one started,
        cancelling the WRONG response. Recording the cancelled id lets
        `_handle_function_call` refuse to execute a function call that
        belongs to a response we already cancelled.
        """
        if not self._active_response_id:
            return
        self._cancelled_response_ids.add(self._active_response_id)
        self._active_response_id = None
        await self._send({"type": "response.cancel"})

    def _unverified_success_replacement(self) -> str:
        if self.session.booking_status == "conflict":
            return (
                "That time is not available on the calendar. Nothing has been booked. "
                "I can check another time if you'd like."
            )
        return (
            "I haven't completed that appointment yet. I could not complete it from "
            "that wording. Let me make sure it is actually booked before I confirm it."
        )

    async def _replace_blocked_confirmation_speech(self, spoken: str) -> None:
        """Cut fake success audio and speak a complete backend-owned sentence."""
        logger.error(
            "call %s: blocked unsupported booking confirmation; status=%s text=%r",
            self.call_id,
            self.session.booking_status,
            spoken[:240],
        )
        self._blocked_unverified_success = True
        replacement = self._unverified_success_replacement()
        self._pending_agent = replacement
        had_response = self._active_response_id is not None
        await self._cancel_active_response()
        if had_response:
            self._pending_forced_tool_message = replacement
            self._response_needed_after_tool = False
            self._restricted_response_needed_after_tool = False
        else:
            await self._send_force_message(replacement)

    def _store(self) -> CallStateStore:
        return CallStateStore(redis_manager.client)

    @property
    def _call_log_sid(self) -> str:
        """Key of the `call_logs` row this session finalizes.

        On the SIP path the call is identified only by an xAI call_id, so the
        row is written under an `xai:` prefix. The media-stream bridge overrides
        this: there the row already exists under the real Twilio CallSid, and
        looking it up prefixed silently finds nothing — losing the transcript.
        """
        return xai_call_sid(self.call_id)

    # ------------------------------------------------------------- transcript
    def _flush_caller_turn(self) -> None:
        """Commit the buffered caller utterance as one history turn.

        The transcription events are cumulative, so the buffer is overwritten
        rather than appended and only lands in history at a turn boundary —
        otherwise a single sentence would appear once per partial.
        """
        text = self._pending_caller.strip()
        self._pending_caller = ""
        if not text:
            return
        history = self.session.history
        if history and history[-1]["role"] == "user" and history[-1]["content"] == text:
            return
        self.session.add_turn("user", text)
        # A genuinely new caller turn is what can ground the NEXT exact-time
        # availability probe — see `_is_time_grounded`.
        self._user_turn_count += 1
        mark_conversation_active(self.session)
        # A new turn is new information: the loop-breakers below are scoped
        # to "no new information since the last rejection", so they reset
        # here, not on a timer or a response boundary.
        if self._rejected_probe_signatures_this_turn:
            logger.info(
                "call %s: new caller turn; clearing %d rejected probe signature(s)",
                self.call_id,
                len(self._rejected_probe_signatures_this_turn),
            )
        self._rejected_probe_signatures_this_turn = set()
        self._tool_chain_depth_this_turn = 0
        self._hold_ack_played_this_turn = False

        # A newly stated time replaces the previous caller-grounded exact time.
        # Do NOT clear on a plain "yes"/"that works" turn: the already-selected
        # provider slot must remain reusable through confirmation/recheck.
        if _TIME_MENTION_RE.search(text):
            if self._grounded_exact_times:
                logger.info(
                    "call %s: caller stated a new time; clearing %d previously grounded exact time(s)",
                    self.call_id,
                    len(self._grounded_exact_times),
                )
            self._grounded_exact_times.clear()

    def _flush_agent_turn(self) -> None:
        """Commit the accumulated agent utterance as one history turn.

        Unlike the caller's transcript these events are incremental, so the
        buffer is appended to and joined at the end of the response.
        """
        text = self._pending_agent.strip()
        self._pending_agent = ""
        if not text:
            return
        if (
            self._should_cancel_restart_greeting()
            and self._looks_like_session_restart_greeting(text)
        ):
            truth(
                "CALL_GREETING_SUPPRESSED",
                call_sid=self.call_id,
                reason="already_greeted",
            )
            return
        history = self.session.history
        if history and history[-1]["role"] == "assistant" and history[-1]["content"] == text:
            return
        self.session.add_turn("assistant", text)

    async def _persist_session(self) -> None:
        await self._store().save(self.session)

    @property
    def _booking_is_persisted(self) -> bool:
        """Whether this call actually has an appointment on a calendar.

        Gates the claim guard below. It previously tested only for status
        "booked", so a caller who moved their time — status "rescheduled" —
        had the agent's perfectly correct "you're all set for the 22nd"
        cancelled and replaced with an apology.
        """
        return (
            self.session.booking_status
            in {BookingOutcome.BOOKED.value, BookingOutcome.RESCHEDULED.value}
            and self.session.appointment_id is not None
        )

    def _mark_pending_booking_read_back(self, assistant_text: str) -> bool:
        """Record that the current provider-checked draft was actually offered.

        `stage_booking` deliberately stops at ``awaiting_confirmation``.  We only
        arm automatic caller-side confirmation after the assistant has asked an
        explicit approval question, so an unrelated "yes" cannot write a booking.
        """
        if self.session.booking_status != BookingOutcome.DRAFT.value and self.session.booking_status != "awaiting_confirmation":
            return False
        draft = get_draft(self.session)
        if not draft.is_complete or not draft.selected_slot:
            return False
        if not _CONFIRMATION_QUESTION_RE.search(assistant_text or ""):
            return False
        if not draft.read_back:
            mark_read_back(self.session)
            logger.info(
                "call %s: pending appointment read-back armed for caller confirmation",
                self.call_id,
            )
        return True

    async def _confirm_pending_booking_from_caller(self, caller_text: str) -> bool:
        """Commit a provider-checked draft directly from an explicit caller yes.

        This is the hard backstop for the live failure where the model verbally
        confirmed a booking but never called ``confirm_appointment``. Once a real
        read-back has been recorded, the state machine—not the LLM—owns the write.
        """
        if self.session.booking_status != "awaiting_confirmation":
            return False
        draft = get_draft(self.session)
        if (
            self.session.entities.get("card_policy_explained")
            and looks_card_hesitant(caller_text)
            and not utterance_modifies_booking(caller_text)
        ):
            await self._send_force_message(CARD_ON_FILE_HESITANT)
            return True
        if utterance_modifies_booking(caller_text) or not is_affirmative(caller_text):
            if utterance_modifies_booking(caller_text):
                logger.info(
                    "call %s: CONFIRMATION_REJECTED reason=modification_present",
                    self.call_id,
                )
            return False
        if caller_name_required_for(draft) and not supplied_caller_name(draft):
            await self._ask_for_caller_name(again=True)
            return True
        if self._card_policy_still_required(draft):
            await self._explain_card_policy()
            return True
        if not draft.read_back or not draft.is_complete or not draft.selected_slot:
            logger.warning(
                "call %s: affirmative heard while appointment is pending but read-back/provider slot is not armed; refusing auto-confirm",
                self.call_id,
            )
            return False
        if not record_pure_confirmation(self.session, caller_text):
            return False

        logger.info(
            "call %s: caller explicitly affirmed provider-checked read-back -> backend confirm",
            self.call_id,
        )
        # xAI may already have auto-created a response as soon as VAD ended.
        # Cancel it before it can improvise a success sentence or emit a duplicate
        # function call. confirm_booking itself remains idempotent as a second guard.
        await self._cancel_active_response()
        self._response_needed_after_tool = False
        self._restricted_response_needed_after_tool = False
        self._pending_forced_tool_message = None

        output = await self._run_confirm_appointment("{}")
        forced = self._authoritative_tool_followup(CONFIRM_APPOINTMENT_TOOL["name"], output)
        if forced is not None:
            logger.info(
                "call %s: backend confirmation succeeded -> authoritative force_message",
                self.call_id,
            )
            self._awaiting_wrap_up = True
            await self._send_force_message(forced, protect_playback=True)
            return True

        # The provider did not prove a successful write. Never let the model turn
        # that into a success claim. Give the caller a deterministic failure line
        # and leave the draft/state available for a safe retry or new selection.
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        status = str(payload.get("status") or "failed")
        message = str(payload.get("message") or "")
        if "caller has not given their name" in message:
            await self._ask_for_caller_name(again=True)
            return True
        if CARD_ON_FILE_POLICY in message:
            await self._explain_card_policy()
            return True
        logger.error(
            "call %s: backend confirm did not produce an authoritative booking status=%s output=%s",
            self.call_id, status, output[:500],
        )
        await self._send_force_message(
            "I wasn't able to complete that appointment, so it is not booked yet. "
            "Would you like me to try again?"
        )
        return True

    def _card_policy_still_required(self, draft) -> bool:
        return bool(
            caller_name_required_for(draft)
            and self.session.entities.get("card_on_file_required")
            and not self.session.entities.get("card_policy_explained")
        )

    def _next_collection_line(self) -> str | None:
        """Name first, then the card policy. Neither repeats once it is stored."""
        recover_caller_name_from_history(self.session)
        draft = get_draft(self.session)
        if not draft.selected_slot or not draft.provider_verified:
            return None
        if caller_name_required_for(draft) and not supplied_caller_name(draft):
            return CALLER_NAME_QUESTION
        if self._card_policy_still_required(draft):
            return CARD_ON_FILE_POLICY
        return None

    async def _ask_for_caller_name(self, *, again: bool = False) -> None:
        recover_caller_name_from_history(self.session)
        draft = get_draft(self.session)
        if not caller_name_required_for(draft) or supplied_caller_name(draft):
            self.session.entities.pop("awaiting_caller_name", None)
            return
        if self._caller_name_question_sent and not again:
            self.session.entities["awaiting_caller_name"] = True
            return
        if again and self._caller_name_reask_turn == self._user_turn_count:
            return
        self._caller_name_reask_turn = self._user_turn_count
        self.session.entities["awaiting_caller_name"] = True
        self._caller_name_question_sent = True
        if self.session.booking_status not in {"booked", "rescheduled"}:
            self.session.booking_status = "collecting_details"
        truth("CALLER_NAME_REQUIRED", call_sid=self.call_id)
        await self._send_force_message(CALLER_NAME_QUESTION)

    async def _explain_card_policy(self) -> None:
        self.session.entities["card_policy_explained"] = True
        if self.session.booking_status not in {"booked", "rescheduled", "conflict"}:
            self.session.booking_status = "awaiting_confirmation"
        truth("CARD_POLICY_SPOKEN", call_sid=self.call_id)
        await self._send_force_message(CARD_ON_FILE_POLICY)

    async def _speak_collection_line(self, line: str) -> None:
        if line == CALLER_NAME_QUESTION:
            await self._ask_for_caller_name()
            return
        if line == CARD_ON_FILE_POLICY:
            await self._explain_card_policy()

    def _is_unauthorized_availability_claim(self, text: str) -> bool:
        """Availability-side counterpart to the booking-confirmation guard
        above. Scoped to Square tenants — the only provider this codebase
        currently returns `AvailabilityVerdict.slot` from — so a
        Google-Calendar/local tenant with no such state is never affected.
        """
        if self.session.entities.get("booking_provider") != "square":
            return False
        return contains_unauthorized_availability_claim(text, self.session, self._tz)

    def _mark_timing(self, stage: str) -> None:
        if self._turn_started_at is None or stage in self._turn_timings:
            return
        elapsed_ms = (time.perf_counter() - self._turn_started_at) * 1000
        self._turn_timings[stage] = elapsed_ms
        logger.info("TURN LATENCY call=%s %s: %.0f ms", self.call_id, stage, elapsed_ms)

    def _start_turn(self) -> None:
        self._turn_started_at = time.perf_counter()
        self._turn_timings = {}

    def _finish_turn(self) -> None:
        if self._turn_started_at is None:
            return
        total_ms = (time.perf_counter() - self._turn_started_at) * 1000
        logger.info(
            "TURN LATENCY call=%s VAD END: %.0f ms STT: %.0f ms "
            "LLM FIRST TOKEN: %.0f ms LLM COMPLETE: %.0f ms "
            "TTS FIRST AUDIO: %.0f ms TOTAL TIME TO FIRST AUDIO: %.0f ms",
            self.call_id,
            self._turn_timings.get("VAD END", 0),
            self._turn_timings.get("STT", 0),
            self._turn_timings.get("LLM FIRST TOKEN", 0),
            self._turn_timings.get("LLM COMPLETE", 0),
            self._turn_timings.get("TTS FIRST AUDIO", 0),
            self._turn_timings.get("TTS FIRST AUDIO", total_ms),
        )
        self._turn_started_at = None

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _booking_write_protocol_instructions() -> str:
        """Non-negotiable scheduling rules shared by SIP and Twilio bridge paths."""
        return (
            "\n\nCRITICAL APPOINTMENT WRITE PROTOCOL:\n"
            "- A spoken promise is NEVER a booking. When the caller supplies or changes a "
            "service/date/time, call propose_appointment before claiming availability.\n"
            "- After propose_appointment says the request is available/pending, the backend "
            "speaks the Square result. If the caller has not given their name on this call, "
            "the backend asks for it before any booking confirmation.\n"
            "- A phone number, a known profile, or the Square customer matched by phone is "
            "not the caller's name. Do not skip the name question because a customer record "
            "already exists. Do not ask again when caller_name is already stored.\n"
            "- When the caller gives their name, pass it as caller_name on propose_appointment "
            "with the same service and time. Do not rename the Square customer and do not "
            "create a new Square customer only because the spoken name differs.\n"
            "- When the caller explicitly agrees (for example yes, that works, book it) after "
            "the name is stored and the details were read back, your NEXT action must be "
            "confirm_appointment. Do not speak a success acknowledgment first.\n"
            "- Never say or imply booked, confirmed, scheduled, reserved, all set, got you down, "
            "good to go, on the calendar, or equivalent success language unless the backend has "
            "returned status=booked or status=rescheduled with an external_booking_id. Final success "
            "is spoken by the backend force_message, not improvised by you.\n"
            "- If a tool fails or no provider booking ID is returned, clearly say the booking was "
            "NOT completed and ask how the caller would like to proceed.\n"
            "- Final booking confirmation is spoken by the backend. Mention a secure card text "
            "only when that confirmation says the text was sent. Do not claim a text was sent "
            "on your own, and do not say the text is what created the booking.\n"
            "- For a Square-backed spa, operational booking facts must come from the booking tools/"
            "provider result, never from memory or guesswork: service existence/variation, duration, "
            "location, staff, availability, appointment time, and booking/cancel/reschedule success. "
            "If the provider has not verified an operational fact yet, ask or call the appropriate "
            "tool instead of asserting it."
        )

    async def _configure(self) -> None:
        instructions = build_realtime_instructions(
            self.session.business_name or settings.APP_NAME,
            self.session.tenant_prompt,
            self.session.timezone,
        )
        instructions += self._booking_write_protocol_instructions()
        known_contact = self.session.entities.get("known_contact")
        if known_contact and known_contact.get("name") not in {None, "Unknown"}:
            instructions += (
                f"\nKNOWN PROFILE: {known_contact['name']}. Caller ID already provides their "
                "phone number; do not ask for the phone again unless they request a different "
                "callback number. This profile is not the name for the booking. Ask for the "
                "caller's name unless caller_name is already stored from this call."
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
                    "tools": VOICE_TOOLS,
                    # Keep voice pacing stable across server/model updates.
                    "audio": {"output": {"speed": 1.0}},
                },
            }
        )

    def _looks_like_session_restart_greeting(self, text: str | None) -> bool:
        """True when the model is restarting the call with the opening welcome."""
        raw = (text or "").strip()
        if not raw:
            return False
        if _GREET_THEM_PROMPT.lower() in raw.lower():
            return True
        return bool(_SESSION_RESTART_GREETING_RE.search(raw))

    def _should_suppress_opening_greeting(self) -> bool:
        """True only after opening audio entered the outbound path."""
        return bool(self.session.greeting_sent)

    def _startup_greeting_open(self) -> bool:
        phase = self.session.phase or CALL_PHASE_NEW
        return (not self.session.greeting_sent) and phase in {
            CALL_PHASE_NEW,
            CALL_PHASE_GREETING_REQUESTED,
        }

    def _is_tracked_greeting_response(self, event: dict[str, Any] | None = None) -> bool:
        rid = self._response_id_of(event) if event else self._active_response_id
        if self._greeting_response_id and rid == self._greeting_response_id:
            return True
        return bool(self._greeting_pending and not self.session.greeting_sent)

    def _outbound_media_ready(self) -> bool:
        """SIP audio is hosted by xAI; Twilio path overrides this."""
        return True

    def _should_cancel_restart_greeting(self, event: dict[str, Any] | None = None) -> bool:
        if not self.session.greeting_sent:
            return False
        rid = self._response_id_of(event) if event else self._active_response_id
        if self._greeting_response_id and rid == self._greeting_response_id:
            return False
        return self._user_turn_count > 0

    async def _note_greeting_audio_started(self, response_id: str | None = None) -> None:
        if self.session.greeting_sent:
            return
        if not (self._greeting_pending or self.session.greeting_requested):
            return
        if response_id:
            self._greeting_response_id = response_id
        self._greeting_got_audio = True
        self._greeting_pending = False
        self._greeting_request_inflight = False
        self._greeted = True
        self._cancel_greeting_watchdog()
        mark_greeting_sent(self.session, response_id=response_id)
        await self._persist_session()

    def _cancel_greeting_watchdog(self) -> None:
        task = self._greeting_watchdog_task
        self._greeting_watchdog_task = None
        if task is not None and not task.done():
            task.cancel()

    def _arm_greeting_watchdog(self) -> None:
        if self._ws is None:
            return
        self._cancel_greeting_watchdog()
        self._greeting_watchdog_task = asyncio.create_task(self._greeting_watchdog())

    async def _greeting_watchdog(self) -> None:
        try:
            await asyncio.sleep(self.GREETING_WATCHDOG_SECONDS)
        except asyncio.CancelledError:
            return
        if self.session.greeting_sent or self._greeting_got_audio:
            return
        if not self._startup_greeting_open() or self._greeting_fallback_used:
            return
        truth(
            "CALL_GREETING_FAILED",
            call_sid=self.call_id,
            reason="no_audio",
        )
        await self._greet_via_model()

    async def _await_xai_session_ready(self, timeout: float = 5.0) -> None:
        """Read until session.created, or continue after a bounded wait."""
        if self._xai_session_ready.is_set() or self._ws is None:
            return
        deadline = time.monotonic() + timeout
        while not self._xai_session_ready.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("call %s: xAI session.created not seen; greeting anyway", self.call_id)
                return
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                logger.warning("call %s: timed out waiting for xAI session.created", self.call_id)
                return
            except Exception:
                return
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            etype = event.get("type") if isinstance(event, dict) else None
            if etype in _FUNCTION_CALL_DONE:
                task = asyncio.create_task(self._dispatch(event))
                self._inflight_tools.add(task)
                task.add_done_callback(self._inflight_tools.discard)
            else:
                await self._dispatch(event)

    async def _suppress_restart_greeting(self, text: str, *, reason: str) -> None:
        truth(
            "CALL_GREETING_SUPPRESSED",
            call_sid=self.call_id,
            reason=reason,
        )
        logger.warning(
            "call %s: suppressed restart greeting reason=%s text=%r",
            self.call_id,
            reason,
            (text or "")[:180],
        )
        await self._cancel_active_response()
        self._pending_agent = ""

    async def _greet(self) -> None:
        """Request the opening greeting once the outbound path is ready.

        Sending the websocket frame is not delivery. greeting_sent is set only
        when greeting audio is accepted by the outbound media path.
        """
        if self.session.greeting_sent:
            truth(
                "CALL_GREETING_SUPPRESSED",
                call_sid=self.call_id,
                reason="already_greeted",
            )
            self._greeted = True
            return
        if self._greeting_request_inflight and not self._greeting_fallback_used:
            truth(
                "CALL_GREETING_SUPPRESSED",
                call_sid=self.call_id,
                reason="already_requested",
            )
            return
        if not self._outbound_media_ready():
            truth(
                "CALL_GREETING_FAILED",
                call_sid=self.call_id,
                reason="stream_not_ready",
            )
            return
        greeting = None
        if self.session.history:
            first = self.session.history[0]
            if first.get("role") == "assistant":
                greeting = first.get("content")
        if not greeting:
            greeting = (
                "Hi, this is Cara, the AI assistant with 6DM. Is now a good time to talk?"
                if self.session.direction == "outbound" else
                f"Thank you for calling {self.session.business_name}. "
                "How may I assist you today?"
            )
        truth("CALL_GREETING_TRIGGER", call_sid=self.call_id)
        mark_greeting_requested(self.session, method="force_message")
        self._greeting_pending = True
        self._greeting_request_inflight = True
        self._greeting_got_audio = False
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "force_message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": greeting}],
                },
            }
        )
        self._greeted = True
        await self._persist_session()
        self._arm_greeting_watchdog()

    async def _greet_via_model(self) -> None:
        """Startup-only fallback if force_message produces no greeting audio."""
        if self.session.greeting_sent:
            truth(
                "CALL_GREETING_SUPPRESSED",
                call_sid=self.call_id,
                reason="already_greeted",
            )
            return
        if not self._startup_greeting_open() or self._greeting_fallback_used:
            truth(
                "CALL_GREETING_SUPPRESSED",
                call_sid=self.call_id,
                reason="already_greeted" if not self._startup_greeting_open() else "fallback_already_used",
            )
            return
        if not self._outbound_media_ready():
            truth(
                "CALL_GREETING_FAILED",
                call_sid=self.call_id,
                reason="stream_not_ready",
            )
            return
        self._greeting_fallback_used = True
        logger.warning(
            "call %s: force_message greeting produced no audio, falling back to model open",
            self.call_id,
        )
        truth("CALL_GREETING_FALLBACK", call_sid=self.call_id, method="model")
        if not self.session.greeting_requested:
            mark_greeting_requested(self.session, method="model")
        self._greeting_pending = True
        self._greeting_got_audio = False
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "[The callee just answered your outbound call. Introduce yourself as Cara, the AI assistant with 6DM, and ask if now is a good time.]"
                                if self.session.direction == "outbound" else _GREET_THEM_PROMPT
                            ),
                        }
                    ],
                },
            }
        )
        await self._send({"type": "response.create"})
        await self._persist_session()
        self._arm_greeting_watchdog()

    # ------------------------------------------------------------- tool calls
    async def _run_manage_appointment(self, raw_arguments: str) -> str:
        """Legacy single-step tool. Must never create a provider booking."""
        return json.dumps({
            "status": "rejected",
            "booked": False,
            "message": (
                "manage_appointment cannot create or change a calendar booking. "
                "Use propose_appointment to check availability, then confirm_appointment "
                "only after the caller gives a pure confirmation of the current proposal."
            ),
        })

    async def _run_propose_appointment(self, raw_arguments: str) -> str:
        """Stage a new booking or move an explicitly selected existing one."""
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            args = {}

        operation = str(args.pop("operation", "") or "").lower()
        appointment_id = args.pop("appointment_id", None)
        if operation not in {"schedule", "reschedule"}:
            operation = (
                "reschedule"
                if _RESCHEDULE_REQUEST_RE.search(self._last_user_utterance() or "")
                else "schedule"
            )

        try:
            intent = AppointmentIntent(confidence=1.0, intent=operation, **args)
        except Exception:
            logger.exception("call %s: invalid proposal %r", self.call_id, raw_arguments)
            return json.dumps({
                "status": "invalid",
                "booked": False,
                "message": "Those details were unclear. Ask for the service, date and time.",
            })

        if operation == "reschedule":
            async with AsyncSessionLocal() as db:
                upcoming = await lookup_upcoming_appointments(db, self.session, limit=10)
            by_id = {str(appt.id): appt for appt in upcoming}
            draft = get_draft(self.session)
            current_id = draft.appointment_id or self.session.appointment_id

            if appointment_id:
                if appointment_id != current_id and appointment_id not in by_id:
                    draft.operation_mode = "reschedule"
                    draft.offered_appointment_ids = list(by_id)
                    draft.target_appointment_id = None
                    save_draft(self.session, draft)
                    await self._persist_session()
                    return json.dumps({
                        "status": "invalid_selection",
                        "booked": False,
                        "appointment_options": [
                            {
                                "appointment_id": str(appt.id),
                                "start_iso": appt.start_time.isoformat(),
                                "service": appt.title,
                            }
                            for appt in upcoming
                        ],
                        "message": "That appointment was not one of the backend-offered choices. Ask the caller which listed appointment they want to move.",
                    })
                draft.target_appointment_id = appointment_id
                draft.offered_appointment_ids = list(by_id)
            elif current_id:
                draft.target_appointment_id = current_id
            elif len(upcoming) == 1:
                draft.target_appointment_id = str(upcoming[0].id)
                draft.offered_appointment_ids = [str(upcoming[0].id)]
            elif len(upcoming) > 1:
                draft.operation_mode = "reschedule"
                draft.target_appointment_id = None
                draft.offered_appointment_ids = list(by_id)
                save_draft(self.session, draft)
                await self._persist_session()
                return json.dumps({
                    "status": "needs_appointment_selection",
                    "booked": False,
                    "appointment_options": [
                        {
                            "appointment_id": str(appt.id),
                            "start_iso": appt.start_time.isoformat(),
                            "service": appt.title,
                        }
                        for appt in upcoming
                    ],
                    "message": "The caller has multiple upcoming appointments. Read the choices back and ask which one to reschedule before staging the new time.",
                })
            else:
                return json.dumps({
                    "status": "not_found",
                    "booked": False,
                    "message": (
                        "No upcoming appointment was found to reschedule. Do NOT create a "
                        "new appointment unless the caller explicitly asks to book a new one."
                    ),
                })

            draft.operation_mode = "reschedule"
            save_draft(self.session, draft)

        checks_square = bool(args.get("requested_start_iso") or args.get("earliest"))
        if checks_square:
            await self._arm_availability_hold()
        try:
            async with AsyncSessionLocal() as db:
                result = await stage_booking(db, self.session, intent)
        finally:
            if checks_square:
                self._availability_lookup_open = False

        apply_booking_result(self.session, result)
        await self._persist_session()
        draft = get_draft(self.session)
        spoken = None
        if checks_square and result.outcome in {BookingOutcome.DRAFT, BookingOutcome.CONFLICT}:
            spoken = authoritative_availability_speech(self.session, self._tz)
        message = result.message
        if spoken:
            message = (
                "The caller already heard the Square availability result. "
                "Do not repeat it and do not offer any other time. "
                "Ask only for a missing name, or call confirm_appointment after a pure yes."
            )
        return json.dumps({
            "status": result.outcome.value,
            "booked": False,
            "operation": draft.operation_mode,
            "target_appointment_id": draft.target_appointment_id,
            "requires_caller_confirmation": result.outcome is BookingOutcome.DRAFT,
            "spoken": spoken,
            "message": message,
        })

    async def _run_confirm_appointment(self, _raw_arguments: str = "") -> str:
        """Commit the pending request. Idempotent; the only tool that writes."""
        draft = get_draft(self.session)
        last = self._last_user_utterance() or ""
        already_booked = draft.is_persisted and self.session.booking_status in {
            BookingOutcome.BOOKED.value, BookingOutcome.RESCHEDULED.value, "booked", "rescheduled"
        }
        if not already_booked:
            if caller_name_required_for(draft) and not supplied_caller_name(draft):
                self.session.entities["awaiting_caller_name"] = True
                self.session.booking_status = "collecting_details"
                return json.dumps({
                    "status": "missing_info",
                    "booked": False,
                    "message": (
                        "Cannot book yet: the caller has not given their name. "
                        "Ask for their name. Do not create the booking."
                    ),
                })
            if self._card_policy_still_required(draft):
                return json.dumps({
                    "status": "missing_info",
                    "booked": False,
                    "message": (
                        "Cannot book yet. Say this to the caller before booking, "
                        f"and do not paraphrase it: {CARD_ON_FILE_POLICY}"
                    ),
                })
            if utterance_modifies_booking(last):
                return json.dumps({
                    "status": "rejected",
                    "booked": False,
                    "message": (
                        "The caller modified the appointment in the same turn. "
                        "Do not book. Apply the change with propose_appointment, "
                        "read the new details back, and wait for a new confirmation."
                    ),
                })
            already_authorized = (
                draft.confirmation_authorized
                and self.session.entities.get("caller_confirmed_revision") == draft.draft_revision
            )
            if not already_authorized and not record_pure_confirmation(self.session, last):
                return json.dumps({
                    "status": "rejected",
                    "booked": False,
                    "message": (
                        "Cannot book until the current proposal is read back and "
                        "the caller explicitly confirms it with no changes."
                    ),
                })

        async with AsyncSessionLocal() as db:
            result = await confirm_booking(db, self.session)

        apply_booking_result(self.session, result)
        if result.appointment is not None:
            self.session.entities["active_appointment_id"] = str(result.appointment.id)
        if result.outcome is not BookingOutcome.SKIPPED:
            self.session.add_turn("system", result.to_system_message())
        await self._persist_session()
        booked = result.outcome in {BookingOutcome.BOOKED, BookingOutcome.RESCHEDULED}
        return json.dumps({
            "status": result.outcome.value,
            "booked": booked,
            "appointment_id": str(result.appointment.id) if result.appointment else None,
            "external_booking_id": (
                result.appointment.external_booking_id if result.appointment else None
            ),
            "card_status": (
                result.appointment.card_status.value
                if result.appointment is not None and getattr(result.appointment, "card_status", None)
                else None
            ),
            "card_sms": result.card_sms,
            "message": result.message,
        })

    async def _run_lookup_appointments(self, raw_arguments: str = "") -> str:
        """Read-only appointment lookup; no Contact or calendar mutation."""
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        purpose = str(args.get("purpose") or "lookup").lower()
        if purpose not in {"lookup", "cancel", "reschedule"}:
            purpose = "lookup"

        async with AsyncSessionLocal() as db:
            upcoming = await lookup_upcoming_appointments(db, self.session, limit=10)

        draft = get_draft(self.session)
        draft.offered_appointment_ids = [str(appt.id) for appt in upcoming]
        if purpose in {"cancel", "reschedule"}:
            draft.operation_mode = purpose
            draft.target_appointment_id = str(upcoming[0].id) if len(upcoming) == 1 else None
        save_draft(self.session, draft)
        await self._persist_session()

        options = [
            {
                "appointment_id": str(appt.id),
                "start_iso": appt.start_time.isoformat(),
                "service": appt.title,
            }
            for appt in upcoming
        ]
        return json.dumps({
            "status": "found" if options else "not_found",
            "read_only": True,
            "purpose": purpose,
            "appointments": options,
            "message": (
                "These are the caller's upcoming appointments, earliest first. For cancel/reschedule, use only an appointment_id from this list."
                if options
                else "No upcoming appointment was found for this caller."
            ),
        })

    async def _run_cancel_appointment(self, raw_arguments: str = "") -> str:
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        appointment_id = args.get("appointment_id")

        async with AsyncSessionLocal() as db:
            result = await cancel_booking(db, self.session, appointment_id=appointment_id)
        apply_booking_result(self.session, result)
        if result.outcome is not BookingOutcome.SKIPPED:
            self.session.add_turn("system", result.to_system_message())
        await self._persist_session()

        draft = get_draft(self.session)
        options: list[dict[str, Any]] = []
        if result.outcome is BookingOutcome.MISSING_INFO and draft.offered_appointment_ids:
            async with AsyncSessionLocal() as db:
                upcoming = await lookup_upcoming_appointments(db, self.session, limit=10)
            allowed = set(draft.offered_appointment_ids)
            options = [
                {
                    "appointment_id": str(appt.id),
                    "start_iso": appt.start_time.isoformat(),
                    "service": appt.title,
                }
                for appt in upcoming
                if str(appt.id) in allowed
            ]

        return json.dumps({
            "status": result.outcome.value,
            "booked": False,
            "appointment_id": str(result.appointment.id) if result.appointment else None,
            "external_booking_id": result.appointment.external_booking_id if result.appointment else None,
            "cancelled_start_iso": result.appointment.start_time.isoformat() if result.outcome is BookingOutcome.CANCELLED and result.appointment else None,
            "service": result.appointment.title if result.appointment else None,
            "appointment_options": options,
            "message": result.message,
        })

    async def _run_start_new_appointment(self, _raw_arguments: str = "") -> str:
        """Open a second, explicitly requested booking intent."""
        start_new_intent(self.session)
        self.session.booking_status = "collecting_details"
        await self._persist_session()
        logger.info("call %s: caller requested a separate second appointment", self.call_id)
        return json.dumps({
            "status": "new_intent",
            "booked": False,
            "message": (
                "Started a separate second appointment. The previous one is unchanged. "
                "Ask for the date, time and service for this additional booking."
            ),
        })

    async def _offer_spoken_window(
        self,
        call_ref: str | None,
        window: tuple[datetime, datetime],
        parsed_args: dict[str, Any],
    ) -> None:
        """Search a morning/afternoon/evening the caller named, not one invented minute."""
        start, end = window
        try:
            intent = AppointmentIntent(
                confidence=1.0,
                intent="schedule",
                caller_name=parsed_args.get("caller_name"),
                caller_email=parsed_args.get("caller_email"),
                service_description=parsed_args.get("service_description"),
                preferred_staff=parsed_args.get("preferred_staff"),
                guest_name=parsed_args.get("guest_name"),
            )
        except Exception:
            intent = AppointmentIntent(confidence=1.0, intent="schedule")
        try:
            async with AsyncSessionLocal() as db:
                result = await search_day_part(db, self.session, intent, start, end)
        except Exception:
            logger.exception("call %s: day-part search failed", self.call_id)
            result_message = (
                "I could not check that part of the day. Ask whether another "
                "day or time would work. Do not invent a time."
            )
            found = False
        else:
            result_message = result.message
            found = "Openings" in result.message
        await self._persist_session()
        await self._send_function_output(
            call_ref,
            json.dumps({
                "status": "day_part_openings" if found else "day_part_empty",
                "available": found,
                "window_start": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "window_end": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "message": result_message,
            }),
        )

    async def _block_unverified_availability(self, spoken: str) -> None:
        """Stop an availability sentence that Square has not verified.

        If a lookup is already running, keep that lookup and let its result
        speak. Otherwise say the last verified times, or a neutral hold line.
        """
        truth(
            "UNVERIFIED_AVAILABILITY_RESPONSE_BLOCKED",
            call_sid=self.call_id,
            reply=(spoken or "")[:180],
        )
        logger.error(
            "call %s: blocked unsupported availability claim; reply=%r",
            self.call_id,
            spoken,
        )
        self._pending_agent = ""
        self._recover_availability_tool = True
        await self._cancel_active_response()
        if self._availability_lookup_open:
            return
        line = grounded_availability_speech(self.session, self._tz)
        await self._send_force_message(line or "One moment while I check the schedule.")

    async def _run_check_availability(self, raw_arguments: str) -> str:
        utterance = self._last_user_utterance() or ""
        if re.search(
            r"\b(repeat|say (?:those|them) again|what were (?:those|the) times)\b",
            utterance,
            re.IGNORECASE,
        ):
            record = get_draft(self.session).verified_availability or {}
            fetched = record.get("fetched_at")
            fresh = False
            if isinstance(fetched, str):
                try:
                    fetched_at = datetime.fromisoformat(fetched)
                    if fetched_at.tzinfo is None:
                        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
                    fresh = (datetime.now(timezone.utc) - fetched_at).total_seconds() <= 90
                except ValueError:
                    fresh = False
            repeated = grounded_availability_speech(self.session, self._tz) if fresh else None
            if repeated:
                truth("AVAILABILITY_REQUEST", call_sid=self.call_id, source="repeat_request")
                return json.dumps({
                    "status": "repeat",
                    "available": True,
                    "spoken": repeated,
                    "message": repeated,
                })
        signature = " ".join((raw_arguments or "").split())
        cached = cached_availability_output(
            self._availability_recent,
            signature,
            time.monotonic(),
            source="exact_check",
        )
        if cached is not None:
            truth(
                "AVAILABILITY_DUPLICATE_SUPPRESSED",
                call_sid=self.call_id,
                source="exact_check",
                request_signature=signature[:160],
            )
            return cached
        try:
            args = json.loads(raw_arguments or "{}")
            intent = AppointmentIntent(confidence=1.0, intent="inquiry", **args)
        except Exception:
            return json.dumps({"status": "failed", "available": False, "message": "Invalid date or time."})
        await self._arm_availability_hold()
        started_generation = get_draft(self.session).draft_revision
        try:
            async with AsyncSessionLocal() as db:
                result = await check_availability_only(db, self.session, intent)
        finally:
            self._availability_lookup_open = False
        if result.stale or get_draft(self.session).draft_revision != started_generation:
            return json.dumps({
                "status": "stale",
                "available": False,
                "lookup_failed": False,
                "message": "The caller changed the request while the schedule was being checked. Check again. Do not offer the previous times.",
            })
        if result.lookup_failed:
            status = "lookup_failed"
            self.session.booking_status = "failed"
        else:
            status = "available" if result.available else "unavailable"
            self.session.booking_status = (
                "awaiting_selection" if result.available else "collecting_details"
            )
        await self._persist_session()
        output = json.dumps({
            "status": status,
            "available": result.available,
            "lookup_failed": result.lookup_failed,
            "spoken": result.message,
            "message": result.message,
        })
        self._availability_recent[signature] = (time.monotonic(), output)
        return output

    async def _run_lookup_spa_facts(self, raw_arguments: str) -> str:
        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        topic = str(args.get("topic") or "all")
        try:
            async with AsyncSessionLocal() as db:
                routing = await _prepare(db, self.session)
                if routing.spa is None:
                    return json.dumps({
                        "status": "unknown",
                        "message": "I don't have that information available.",
                    })
                payload = await lookup_spa_facts(
                    routing.spa,
                    adapter=routing.adapter,
                    topic=topic,
                    service_name=args.get("service_name"),
                    location_query=args.get("location_query"),
                    vip_identifier=args.get("vip_identifier"),
                )
        except Exception:
            logger.exception("call %s: spa fact lookup failed", self.call_id)
            return json.dumps({
                "status": "unknown",
                "message": "I don't have that information available.",
            })
        return json.dumps(payload)

    async def _run_request_callback(self, raw_arguments: str) -> str:
        from app.models.follow_up_request import FollowUpRequest
        from app.services.secure_payment import contains_sensitive_payment
        from app.services.staff_notifications import callback_request_payload, notify_staff

        try:
            args = json.loads(raw_arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        if contains_sensitive_payment(args):
            return json.dumps({
                "status": "rejected",
                "message": "Do not collect card details in a callback request.",
            })
        try:
            async with AsyncSessionLocal() as db:
                routing = await _prepare(db, self.session)
                spa = routing.spa
                payload = callback_request_payload(
                    spa_id=getattr(spa, "id", None),
                    call_sid=self.call_id,
                    caller_phone=self.session.customer_phone,
                    caller_name=args.get("caller_name") or get_draft(self.session).caller_name,
                    reason=args.get("reason"),
                    preferred_window=args.get("preferred_window"),
                )
                if spa is not None:
                    db.add(FollowUpRequest(
                        spa_id=spa.id,
                        call_sid=payload["call_sid"],
                        caller_phone=payload["caller_phone"],
                        caller_name=payload["caller_name"],
                        reason=payload["reason"],
                        preferred_window=payload["preferred_window"],
                        kind="callback",
                        status="open",
                    ))
                    await db.commit()
                    await notify_staff(
                        spa,
                        "callback_requested",
                        f"Callback requested on call {self.call_id}.",
                    )
        except Exception:
            logger.exception("call %s: callback request failed", self.call_id)
            return json.dumps({
                "status": "error",
                "message": "I could not save that callback request.",
            })
        return json.dumps({
            "status": "stored",
            "kind": "callback",
            "message": "The callback request is saved. Tell the caller someone will follow up. Do not invent who or when.",
        })

    # Tools that reach the live scheduling provider by probing one specific
    # instant. Nothing stops the model from calling one of these repeatedly
    # with a different self-guessed time to simulate a search — this is
    # exactly what happened on the live call that motivated this guard: eight
    # straight `check_availability` calls with 30-minute-apart fabricated
    # times, all inside one response. Capped per-response below.
    # MANAGE_APPOINTMENT_TOOL is included here even though it is not currently
    # offered in VOICE_TOOLS (see the module docstring's tool table). It is a
    # legacy single-step tool whose handler still exists and writes directly
    # via attempt_booking with a caller-supplied requested_start_iso. If it is
    # ever re-added to VOICE_TOOLS, this ensures the temporal-grounding guard
    # covers it too, rather than silently bypassing the one tool that can
    # write a booking from a self-invented time.
    _AVAILABILITY_PROBE_TOOLS = frozenset(
        {
            CHECK_AVAILABILITY_TOOL["name"],
            PROPOSE_APPOINTMENT_TOOL["name"],
            MANAGE_APPOINTMENT_TOOL["name"],
        }
    )

    def _last_user_utterance(self) -> str | None:
        for turn in reversed(self.session.history):
            if turn.get("role") == "user":
                return turn.get("content")
        return None

    def _recent_user_utterances(self, limit: int) -> list[str]:
        found: list[str] = []
        for turn in reversed(self.session.history):
            if turn.get("role") != "user":
                continue
            content = turn.get("content")
            if content:
                found.append(content)
            if len(found) >= limit:
                break
        return found

    def _active_established_date(self) -> date | None:
        """The calendar date the caller most recently, unambiguously
        established — for a LATER turn that names only a time to complete.

        Walks backward through the caller's prior turns (not the current
        one) and stops at the first of:
          * an explicit cancellation with no date in the same breath
            ("never mind that", "forget it") -> no active date;
          * a turn naming a date -> that date, best-effort parsed. Stopping
            at the first (most recent) hit is what lets a correction
            ("actually, make that October 2nd") supersede an earlier date
            instead of both being "recent" and ambiguous.
        Returns None rather than guessing if the date can't be confidently
        parsed, or if nothing recent enough establishes one at all — see
        `_RECENT_DATE_CONTEXT_TURNS`.
        """
        today = datetime.now(self._tz).date()
        recent = self._recent_user_utterances(_RECENT_DATE_CONTEXT_TURNS + 1)
        for utterance in recent[1:]:  # [0] is the current turn itself
            has_date = _DATE_MENTION_RE.search(utterance)
            if _DATE_CANCEL_RE.search(utterance) and not has_date:
                return None
            if has_date:
                # The most recent date-naming turn wins outright, even if it
                # can't be confidently resolved to a value — falling through
                # to an OLDER turn here would let a vague correction
                # ("actually, a different day") un-cancel a stale date.
                return _extract_explicit_date(utterance, today)
        return None

    def _spoken_date(self, utterance: str | None, today: date) -> tuple[date | None, str]:
        """Date the caller named, and whether it came from a weekday."""
        text = utterance or ""
        explicit = _extract_explicit_date(text, today)
        if explicit is not None and _extract_weekday_date(text, today) != explicit:
            return explicit, "explicit"
        if _extract_weekday_date(text, today) is not None:
            return _extract_weekday_date(text, today), "weekday"
        if explicit is not None:
            return explicit, "explicit"
        active = self._active_established_date()
        return active, "active" if active is not None else "none"

    def _prior_date_is_weekday(self) -> bool:
        """True when the date still in play came from a weekday name, not a calendar date."""
        today = datetime.now(self._tz).date()
        recent = self._recent_user_utterances(_RECENT_DATE_CONTEXT_TURNS + 1)
        for utterance in recent[1:]:
            has_date = bool(_DATE_MENTION_RE.search(utterance))
            if _DATE_CANCEL_RE.search(utterance) and not has_date:
                return False
            if has_date:
                weekday = _extract_weekday_date(utterance, today)
                resolved = _extract_explicit_date(utterance, today)
                return weekday is not None and resolved == weekday
        return False

    def _spoken_exact_start(self) -> datetime | None:
        """Local datetime for a weekday or date plus a clock time the caller said.

        The model's own ISO is not used. "Thursday at 4pm" becomes the next
        Thursday at 16:00 in the spa timezone. If that moment has already
        passed and the caller named a weekday rather than a calendar date,
        it rolls forward one week.
        """
        utterance = self._last_user_utterance()
        if not utterance:
            return None
        clock = _extract_clock_time(utterance)
        if clock is None:
            return None
        now = datetime.now(self._tz)
        spoken_date, source = self._spoken_date(utterance, now.date())
        # Calendar dates and "tomorrow" stay exactly as the model sent them.
        # Only a weekday ("Thursday at 4pm", or "4pm" after "Thursday") is
        # resolved here, so a wrong model date cannot reach the provider.
        if spoken_date is None or source in {"none", "explicit"}:
            return None
        if source == "active" and not self._prior_date_is_weekday():
            return None
        start = datetime.combine(spoken_date, clock, tzinfo=self._tz)
        if start <= now:
            start = start + timedelta(days=7)
        return start

    def _spoken_day_part_window(self) -> tuple[datetime, datetime] | None:
        """Local window for "Saturday afternoon" when no clock time was said."""
        utterance = self._last_user_utterance()
        if not utterance or _extract_clock_time(utterance):
            return None
        hours = _extract_day_part(utterance)
        if hours is None:
            return None
        now = datetime.now(self._tz)
        spoken_date, source = self._spoken_date(utterance, now.date())
        if spoken_date is None or source == "none":
            return None
        start = datetime.combine(spoken_date, hours[0], tzinfo=self._tz)
        end = datetime.combine(spoken_date, hours[1], tzinfo=self._tz)
        weekday_based = source == "weekday" or (
            source == "active" and self._prior_date_is_weekday()
        )
        if weekday_based and end <= now:
            start = start + timedelta(days=7)
            end = end + timedelta(days=7)
        return start, end

    def _caller_requested_earliest(self) -> bool:
        """Tool args are not proof that the caller requested an earliest search."""
        utterance = self._last_user_utterance() or ""
        return bool(_EARLIEST_REQUEST_RE.search(utterance))

    def _time_key(self, requested_start_iso: str | None) -> str | None:
        """Canonical UTC key for comparing caller/provider exact timestamps."""
        if not requested_start_iso:
            return None
        target = _parse_dt(requested_start_iso, self._tz)
        if target is None:
            return None
        return target.astimezone(timezone.utc).isoformat()

    def _is_time_grounded(self, requested_start_iso: str | None) -> bool:
        """Whether an exact-time probe has real evidence behind it.

        Allowed sources:
        1. the caller's current fresh turn explicitly mentions a time of day;
        2. the exact timestamp already passed grounding earlier for that same
           caller-stated time and is merely being retried;
        3. the exact timestamp matches a provider-confirmed offered/pinned slot.

        Crucially, (2) is exact-instant only. A retry of 4:00 PM may reuse the
        prior grounding; 4:30 PM or another date does not inherit permission.
        When the caller states a new time, `_flush_caller_turn` clears the old
        durable timestamp(s).
        """
        key = self._time_key(requested_start_iso)
        if key is None:
            return False

        # Same exact caller-grounded instant may be retried without forcing the
        # caller to repeat themselves.
        if key in self._grounded_exact_times:
            logger.info(
                "call %s: reusing previously grounded exact time %s",
                self.call_id,
                requested_start_iso,
            )
            return True

        target = _parse_dt(requested_start_iso, self._tz)
        if target is None:
            return False

        # A fresh caller turn that actually contains a time can ground one exact
        # timestamp — but only combined with a date the caller actually named,
        # in this turn or as the most recent unretracted date established
        # earlier. A bare "3pm" with no date anywhere recent, or a date that
        # doesn't match what's actually active (superseded or cancelled),
        # must not be treated as grounding a timestamp on some date the model
        # invented or resurrected. `_handle_function_call` records the
        # accepted timestamp immediately after this check, so later retries
        # must match it exactly.
        if self._user_turn_count > self._last_probe_turn_count:
            last_utterance = self._last_user_utterance()
            if last_utterance and _TIME_MENTION_RE.search(last_utterance):
                spoken = self._spoken_exact_start()
                if spoken is not None:
                    return (
                        target.astimezone(timezone.utc)
                        == spoken.astimezone(timezone.utc)
                    )
                if _DATE_MENTION_RE.search(last_utterance):
                    return True
                active_date = self._active_established_date()
                if active_date is not None and active_date == target.date():
                    return True

        # Provider-returned slots remain authoritative independent of caller
        # transcript wording.
        draft = get_draft(self.session)
        candidates = list(draft.alternative_slots or [])
        if draft.selected_slot:
            candidates.append(draft.selected_slot)
        for slot in candidates:
            slot_dt = _parse_provider_iso(slot.get("start"))
            if (
                slot_dt is not None
                and slot_dt.astimezone(timezone.utc)
                == target.astimezone(timezone.utc)
            ):
                return True
        return False

    def _remember_grounded_exact_time(self, requested_start_iso: str | None) -> None:
        """Remember only the exact timestamp that just passed the grounding gate."""
        key = self._time_key(requested_start_iso)
        if key is None:
            return
        if key not in self._grounded_exact_times:
            logger.info(
                "call %s: remembering grounded exact time %s",
                self.call_id,
                requested_start_iso,
            )
        self._grounded_exact_times.add(key)

    async def _send_force_message(self, message: str, *, protect_playback: bool = False) -> None:
        """Speak an authoritative backend result verbatim without another model turn.

        xAI's `force_message` synthesizes the supplied text directly and creates
        its own normal response lifecycle. Do not follow it with response.create.
        This is ideal for final booking confirmations because the calendar result,
        not the language model, is the source of truth.
        """
        if protect_playback:
            self._protect_playback = True
        if "anything else I can help you with today" in message:
            self._awaiting_wrap_up = True
        if (
            self._should_cancel_restart_greeting()
            and self._looks_like_session_restart_greeting(message)
        ):
            truth(
                "CALL_GREETING_SUPPRESSED",
                call_sid=self.call_id,
                reason="already_greeted",
            )
            return
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "force_message",
                    "role": "assistant",
                    "interruptible": True,
                    "content": [{"type": "output_text", "text": message}],
                },
            }
        )

    def _authoritative_tool_followup(self, tool_name: str, output: str) -> str | None:
        """Return exact caller-facing speech for terminal authoritative outcomes."""
        try:
            payload = json.loads(output)
        except (TypeError, json.JSONDecodeError):
            return None

        status = str(payload.get("status") or "").lower()

        if tool_name == CANCEL_APPOINTMENT_TOOL["name"]:
            if status != "cancelled" or not payload.get("appointment_id"):
                return None
            service = str(payload.get("service") or "appointment").strip()
            cancelled_start = payload.get("cancelled_start_iso")
            if not cancelled_start:
                return (
                    f"Your {service} appointment has been cancelled. "
                    "Would you like to book another time?"
                )
            try:
                dt = datetime.fromisoformat(str(cancelled_start).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                local = dt.astimezone(self._tz)
                day = f"{local.strftime('%A, %B')} {local.day}"
                spoken_time = local.strftime("%I:%M %p").lstrip("0")
                if spoken_time.endswith(":00 AM") or spoken_time.endswith(":00 PM"):
                    spoken_time = spoken_time.replace(":00 ", " ")
                return (
                    f"Your {service} appointment for {day} at {spoken_time} has been cancelled. "
                    "Would you like to book another time?"
                )
            except (TypeError, ValueError):
                return (
                    f"Your {service} appointment has been cancelled. "
                    "Would you like to book another time?"
                )

        if tool_name != CONFIRM_APPOINTMENT_TOOL["name"]:
            return None
        if status not in {"booked", "rescheduled"}:
            return None
        if not payload.get("appointment_id") or not payload.get("external_booking_id"):
            return None

        service = (self.session.selected_service or "appointment").strip()
        provider = (get_draft(self.session).preferred_staff or "").strip()
        with_provider = f" with {provider}" if provider else ""
        confirmed = self.session.confirmed_datetime

        def finish(sentence: str) -> str:
            if payload.get("card_status") == "pending_card":
                sentence = sentence.replace("You're all set. ", "")
                sentence = sentence.replace("is confirmed", "is reserved pending your card on file")
            clause = booking_card_speech(payload.get("card_status"), payload.get("card_sms"))
            question = " Is there anything else I can help you with today?"
            truth(
                "BOOKING_CONFIRMATION_SPOKEN",
                call_sid=self.call_id,
                card_status=payload.get("card_status") or "none",
                card_sms=payload.get("card_sms") or "not_attempted",
            )
            if sentence.endswith(question):
                return sentence[: -len(question)] + clause + question
            return sentence + clause

        if not confirmed:
            # Still avoid an invented greeting even if the local display time is
            # unexpectedly unavailable; provider IDs prove the write succeeded.
            verb = "confirmed" if status == "booked" else "rescheduled"
            return finish(
                f"You're all set. Your {service} appointment{with_provider} is {verb}. "
                "Is there anything else I can help you with today?"
            )

        try:
            dt = datetime.fromisoformat(str(confirmed).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(self._tz)
            day = f"{local.strftime('%A, %B')} {local.day}"
            spoken_time = local.strftime("%I:%M %p").lstrip("0")
            if spoken_time.endswith(":00 AM") or spoken_time.endswith(":00 PM"):
                spoken_time = spoken_time.replace(":00 ", " ")
        except (TypeError, ValueError):
            verb = "confirmed" if status == "booked" else "rescheduled"
            return finish(
                f"You're all set. Your {service} appointment{with_provider} is {verb}. "
                "Is there anything else I can help you with today?"
            )

        self._awaiting_wrap_up = True
        if status == "rescheduled":
            return finish(
                f"You're all set. Your {service} appointment{with_provider} has been moved to "
                f"{day} at {spoken_time}. "
                "Is there anything else I can help you with today?"
            )
        return finish(
            f"You're all set. Your {service} appointment{with_provider} is confirmed for "
            f"{day} at {spoken_time}. "
            "Is there anything else I can help you with today?"
        )

    async def _send_function_output(
        self,
        call_ref: str | None,
        output: str,
        *,
        cache: bool = True,
        nudge: bool = True,
        restrict_continuation: bool = False,
    ) -> None:
        """Send one `function_call_output`, optionally caching it for replay
        on a duplicate `call_id` and nudging the model to continue.

        The nudge is skipped whenever a response is still open — see the
        comment at the bottom of `_handle_function_call`.

        `restrict_continuation` marks this as a guard-rail REJECTION (ungrounded
        time/earliest, per-response cap, duplicate probe, chain-depth limit):
        the model still needs to say something to the caller, but the
        continuation this triggers must not be free to call a tool again —
        that is exactly the autonomous retry loop this exists to prevent. The
        continuation is sent with `tool_choice: "none"` so it can only speak.
        """
        if cache and call_ref:
            self._call_id_outputs[call_ref] = output
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_ref,
                    "output": output,
                },
            }
        )
        if not nudge:
            return

        response_create: dict[str, Any] = {"type": "response.create"}
        if restrict_continuation:
            response_create["response"] = {"tool_choice": "none"}

        if self._active_response_id is None:
            self._log_tool_continuation(
                "issued",
                reason="restricted_followup" if restrict_continuation else "tool_result_complete",
            )
            await self._send(response_create)
        else:
            # The function call belongs to the response that is still open.
            # Starting another response now would overlap/stack responses,
            # so defer exactly one continuation until response.done.
            if restrict_continuation:
                self._restricted_response_needed_after_tool = True
            else:
                self._response_needed_after_tool = True
            self._log_tool_continuation(
                "deferred",
                reason="restricted_followup" if restrict_continuation else "tool_result_complete",
                active_response_id=self._active_response_id,
            )

    def _log_tool_continuation(self, decision: str, **fields: Any) -> None:
        """Structured log line for every response.create decision this
        module makes, so a runaway tool-continuation chain is diagnosable
        from logs alone: `TOOL_CONTINUATION <decision> call=... turn=... ...`.
        """
        parts = " ".join(f"{key}={value}" for key, value in fields.items())
        logger.info(
            "TOOL_CONTINUATION %s call=%s turn=%d chain_depth=%d %s",
            decision,
            self.call_id,
            self._user_turn_count,
            self._tool_chain_depth_this_turn,
            parts,
        )

    async def _arm_availability_hold(self) -> None:
        """Mute the in-flight model response and speak the hold line immediately."""
        self._availability_lookup_open = True
        self._availability_speech_interrupted = False
        self._availability_model_response_id = self._active_response_id
        self._muted_availability_response_id = self._active_response_id
        self._availability_hold_response_id = None
        self._availability_hold_done = False
        self._availability_expect_hold = False
        self._pending_availability_speech = None
        if not self._hold_ack_played_this_turn and self._ws is not None:
            self._hold_ack_played_this_turn = True
            self._availability_expect_hold = True
            await self._send_force_message(HOLD_ACK_TEXT)

    async def _speak_availability(self, spoken: str) -> None:
        """Speak the Square sentence, then name or card policy once it finishes."""
        line = self._next_collection_line()
        self._after_availability_line = line
        self._prompt_caller_name_after_response = line is not None
        await self._send_force_message(spoken)

    async def _deliver_authoritative_availability(self, call_ref: str | None, output: str) -> None:
        """Speak a finished Square result and do not let the model continue."""
        spoken = None
        try:
            spoken = json.loads(output).get("spoken")
        except (TypeError, json.JSONDecodeError):
            spoken = None
        await self._send_function_output(call_ref, output, nudge=False)
        model_response = getattr(self, "_availability_model_response_id", None)
        self._availability_model_response_id = None
        self._muted_availability_response_id = None
        if model_response and self._active_response_id == model_response:
            await self._cancel_active_response()
        elif model_response:
            self._cancelled_response_ids.add(model_response)
        if spoken and not self._availability_speech_interrupted:
            hold_still_open = self._availability_expect_hold or (
                self._availability_hold_response_id is not None
                and not self._availability_hold_done
            )
            if hold_still_open:
                self._pending_availability_speech = str(spoken)
            else:
                await self._speak_availability(str(spoken))

    async def _maybe_hold_ack(self) -> None:
        """Speak once if a provider lookup is still running, then a soft tone."""
        try:
            await asyncio.sleep(self.HOLD_ACK_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        if self._hold_ack_played_this_turn or self._ws is None:
            return
        self._hold_ack_played_this_turn = True
        logger.info("HOLD_ACK call=%s", self.call_id)
        await self._send_force_message(HOLD_ACK_TEXT)
        try:
            await asyncio.sleep(self.HOLD_TONE_DELAY_SECONDS)
        except asyncio.CancelledError:
            return
        start = getattr(self, "start_hold_tone", None)
        if start is not None:
            await start()

    async def _invoke_tool_with_hold(self, name: str, handler, raw_args: str) -> str:
        hold_tools = {
            CHECK_AVAILABILITY_TOOL["name"],
            PROPOSE_APPOINTMENT_TOOL["name"],
            CONFIRM_APPOINTMENT_TOOL["name"],
            LOOKUP_APPOINTMENTS_TOOL["name"],
            CANCEL_APPOINTMENT_TOOL["name"],
            LOOKUP_SPA_FACTS_TOOL["name"],
            MANAGE_APPOINTMENT_TOOL["name"],
        }
        if name not in hold_tools:
            return await handler(raw_args)
        ack = asyncio.create_task(self._maybe_hold_ack())
        try:
            return await handler(raw_args)
        finally:
            ack.cancel()
            try:
                await ack
            except asyncio.CancelledError:
                pass
            stop = getattr(self, "stop_hold_tone", None)
            if stop is not None:
                await stop()

    async def _handle_function_call(self, data: dict[str, Any]) -> None:
        name = _first_str(data, "name", "function_name") or ""
        call_ref = _first_str(data, "call_id", "tool_call_id", "id")
        raw_args = _first_str(data, "arguments", "args") or "{}"
        response_id = self._response_id_of(data)
        logger.info(
            "TOOL_CALL received call=%s turn=%d response_id=%s tool_call_id=%s tool=%s args=%s",
            self.call_id, self._user_turn_count, response_id, call_ref, name, raw_args[:500],
        )

        # A duplicate delivery of a call we already handled. The realtime
        # protocol still expects exactly one function_call_output per
        # call_id, so this replays the cached result instead of silently
        # dropping it — dropping it risks the model waiting on an output it
        # will never see if this was the only delivery it actually noticed.
        # The provider operation itself never runs twice: dispatch is
        # strictly sequential (one event at a time off the socket), so by
        # the time a duplicate can arrive the first call has already
        # finished and its output is cached.
        if call_ref and call_ref in self._call_id_outputs:
            logger.warning("call %s: duplicate tool call_id %r replayed", self.call_id, call_ref)
            await self._send_function_output(
                call_ref, self._call_id_outputs[call_ref], cache=False, nudge=False
            )
            return

        # A function call that arrived for a response we already cancelled
        # (booking-confirmation guard, availability-claim guard, or a
        # caller barge-in) must not still reach the scheduling provider —
        # the conversation already moved on from that response.
        if (
            response_id
            and response_id in self._cancelled_response_ids
            and not (name in self._AVAILABILITY_PROBE_TOOLS and self._recover_availability_tool)
        ):
            logger.warning(
                "call %s: ignoring tool call %r from cancelled response %s",
                self.call_id, name, response_id,
            )
            await self._send_function_output(
                call_ref,
                json.dumps({
                    "status": "cancelled",
                    "message": "This request was superseded; do not act on it.",
                }),
                nudge=False,
            )
            return
        if name in self._AVAILABILITY_PROBE_TOOLS and self._recover_availability_tool:
            self._recover_availability_tool = False

        # Hard ceiling on tool round-trips within one caller turn, independent
        # of every guard above. Even if a guard were somehow bypassed, this
        # bounds the damage instead of relying on any single check being
        # perfect. Counts every real attempt (any tool), not just probes.
        self._tool_chain_depth_this_turn += 1
        if self._tool_chain_depth_this_turn > self.MAX_TOOL_CHAIN_DEPTH_PER_TURN:
            logger.error(
                "TOOL_CONTINUATION blocked call=%s turn=%d tool=%s reason=chain_depth_limit "
                "depth=%d limit=%d",
                self.call_id, self._user_turn_count, name,
                self._tool_chain_depth_this_turn, self.MAX_TOOL_CHAIN_DEPTH_PER_TURN,
            )
            await self._send_function_output(
                call_ref,
                json.dumps({
                    "status": "tool_chain_limit",
                    "available": False,
                    "message": (
                        "Too many attempts this turn. Stop calling tools and ask the "
                        "caller a clarifying question instead."
                    ),
                }),
                nudge=False,
            )
            return

        if name in self._AVAILABILITY_PROBE_TOOLS:
            try:
                parsed_args = json.loads(raw_args or "{}")
            except (TypeError, ValueError):
                parsed_args = {}
            spoken_window = self._spoken_day_part_window()
            if spoken_window is not None and name in {
                CHECK_AVAILABILITY_TOOL["name"],
                PROPOSE_APPOINTMENT_TOOL["name"],
            }:
                await self._offer_spoken_window(call_ref, spoken_window, parsed_args)
                return

            spoken_start = self._spoken_exact_start()
            if spoken_start is not None:
                local_iso = spoken_start.strftime("%Y-%m-%dT%H:%M:%S")
                if parsed_args.get("requested_start_iso") != local_iso:
                    logger.info(
                        "call %s: using caller wording %s instead of model time %s",
                        self.call_id,
                        local_iso,
                        parsed_args.get("requested_start_iso"),
                    )
                parsed_args["requested_start_iso"] = local_iso
                parsed_args["earliest"] = False
                raw_args = json.dumps(parsed_args)

            is_earliest = bool(parsed_args.get("earliest"))
            requested_start = parsed_args.get("requested_start_iso")

            # Same semantically-equivalent probe already rejected this turn
            # (e.g. an offset-qualified duplicate of the same business-local
            # instant) must not be allowed to run the exact same rejection
            # (and its follow-up) over and over — this is the actual
            # loop-breaker, independent of whether the model respects the
            # speech-only continuation below.
            signature = (name, self._time_key(requested_start) if not is_earliest else "earliest")
            if signature in self._rejected_probe_signatures_this_turn:
                logger.error(
                    "TOOL_CONTINUATION blocked call=%s turn=%d tool=%s reason=duplicate_rejected_probe "
                    "args=%s",
                    self.call_id, self._user_turn_count, name, signature,
                )
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "duplicate_probe",
                        "available": False,
                        "message": (
                            "That exact request was already refused this turn. Do not "
                            "retry it — ask the caller a clarifying question instead."
                        ),
                    }),
                    nudge=False,
                )
                return

            if is_earliest and not self._caller_requested_earliest():
                logger.warning(
                    "call %s: refusing ungrounded earliest search; latest caller turn=%r",
                    self.call_id,
                    self._last_user_utterance(),
                )
                self._rejected_probe_signatures_this_turn.add(signature)
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "ungrounded_earliest",
                        "available": False,
                        "message": (
                            "The caller did not ask for the earliest/soonest opening. "
                            "Do not run a new search and do not invent a time. Use the "
                            "exact caller-stated/provider-offered slot, or ask for their "
                            "preferred date and time."
                        ),
                    }),
                    restrict_continuation=True,
                )
                return

            # The core scheduler rule: the model placing a timestamp in tool
            # arguments is NOT evidence the caller asked for it. An exact-time
            # probe must be grounded in a fresh caller turn or a previously
            # offered/pinned authoritative slot — checked here, independent
            # of `response_id`, so a NEW response cannot re-grant permission
            # to keep guessing (the per-response cap below is a secondary
            # safety net, not the correctness rule).
            if not is_earliest and requested_start and not self._is_time_grounded(requested_start):
                logger.warning(
                    "call %s: refusing ungrounded exact-time probe %r (no new caller "
                    "turn, no matching offered slot)",
                    self.call_id, requested_start,
                )
                self._rejected_probe_signatures_this_turn.add(signature)
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "ungrounded_time",
                        "available": False,
                        "message": (
                            "That exact time was not something the caller just said, nor "
                            "one of the options already offered. Ask the caller for a "
                            "date/time, or use earliest=true if they want the soonest "
                            "opening — do not guess another time yourself."
                        ),
                    }),
                    restrict_continuation=True,
                )
                return

            # This probe is grounded (or an explicit earliest search). For an
            # exact-time probe, remember THIS instant before consuming the fresh
            # caller turn. A retry of the same instant remains valid; a different
            # timestamp still cannot ride along on the same turn.
            if not is_earliest and requested_start:
                self._remember_grounded_exact_time(requested_start)
            self._last_probe_turn_count = self._user_turn_count

        if name in self._AVAILABILITY_PROBE_TOOLS and response_id:
            probes = self._availability_probes_by_response.get(response_id, 0) + 1
            self._availability_probes_by_response[response_id] = probes
            if probes > self.MAX_AVAILABILITY_PROBES_PER_RESPONSE:
                logger.warning(
                    "call %s: refusing availability probe #%d in one response (%s)",
                    self.call_id, probes, response_id,
                )
                self._rejected_probe_signatures_this_turn.add(signature)
                await self._send_function_output(
                    call_ref,
                    json.dumps({
                        "status": "too_many_attempts",
                        "available": False,
                        "message": (
                            "Stop trying different times yourself. Ask the caller for a "
                            "specific date/time, or ask if they want the earliest opening, "
                            "before checking again."
                        ),
                    }),
                    restrict_continuation=True,
                )
                return

        handlers = {
            CHECK_AVAILABILITY_TOOL["name"]: self._run_check_availability,
            PROPOSE_APPOINTMENT_TOOL["name"]: self._run_propose_appointment,
            CONFIRM_APPOINTMENT_TOOL["name"]: self._run_confirm_appointment,
            LOOKUP_APPOINTMENTS_TOOL["name"]: self._run_lookup_appointments,
            CANCEL_APPOINTMENT_TOOL["name"]: self._run_cancel_appointment,
            NEW_APPOINTMENT_TOOL["name"]: self._run_start_new_appointment,
            MANAGE_APPOINTMENT_TOOL["name"]: self._run_manage_appointment,
            LOOKUP_SPA_FACTS_TOOL["name"]: self._run_lookup_spa_facts,
            REQUEST_CALLBACK_TOOL["name"]: self._run_request_callback,
        }
        handler = handlers.get(name)
        if handler is None:
            logger.warning("call %s: unknown tool %r requested", self.call_id, name)
            output = f"Tool {name!r} is not available."
        else:
            output = await self._invoke_tool_with_hold(name, handler, raw_args)

        if name == CHECK_AVAILABILITY_TOOL["name"] or (
            name == PROPOSE_APPOINTMENT_TOOL["name"]
            and '"spoken":' in output
            and json.loads(output).get("spoken")
        ):
            await self._deliver_authoritative_availability(call_ref, output)
            return

        # Successful booking confirmation is authoritative and terminal: speak
        # an exact backend-derived line rather than asking Grok to invent the
        # next turn (which has been observed to restart with "hello/welcome").
        forced_followup = self._authoritative_tool_followup(name, output)
        if forced_followup is None and name == CONFIRM_APPOINTMENT_TOOL["name"]:
            try:
                confirm_payload = json.loads(output)
            except (TypeError, json.JSONDecodeError):
                confirm_payload = {}
            confirm_message = str(confirm_payload.get("message") or "")
            if "has not given their name" in confirm_message:
                forced_followup = CALLER_NAME_QUESTION
                self.session.entities["awaiting_caller_name"] = True
            elif CARD_ON_FILE_POLICY in confirm_message:
                forced_followup = CARD_ON_FILE_POLICY
                self.session.entities["card_policy_explained"] = True
        if forced_followup is not None:
            await self._send_function_output(call_ref, output, nudge=False)
            if self._active_response_id is None:
                logger.info(
                    "call %s: authoritative %s result -> force_message confirmation",
                    self.call_id,
                    name,
                )
                await self._send_force_message(
                    forced_followup,
                    protect_playback=name == CONFIRM_APPOINTMENT_TOOL["name"],
                )
            else:
                self._pending_forced_tool_message = forced_followup
                logger.info(
                    "call %s: authoritative %s result ready; deferring force_message until response %s closes",
                    self.call_id,
                    name,
                    self._active_response_id,
                )
            return

        # Non-terminal tool results still need an explicit model continuation,
        # but only after the response that requested the tool has closed.
        await self._send_function_output(call_ref, output)

    # ------------------------------------------------------------ event loop
    async def _dispatch(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        data = event.get("data") if isinstance(event.get("data"), dict) else event

        if etype in _AUDIO_DELTA_EVENTS:
            await self._note_greeting_audio_started(
                self._response_id_of(event) or self._response_id_of(data)
            )
            return

        if etype in _SPEECH_STARTED_EVENTS:
            self._start_turn()
            return

        if etype == "input_audio_buffer.speech_stopped":
            self._mark_timing("VAD END")
            return

        if etype in _CALLER_TRANSCRIPT_UPDATED:
            self._mark_timing("STT")
            text = _first_str(data, "transcript", "text", "delta")
            if text:
                self._pending_caller = text
            return

        if etype in _CALLER_TRANSCRIPT_DONE:
            self._mark_timing("STT")
            text = _first_str(data, "transcript", "text")
            if text:
                self._pending_caller = text
            caller_text = self._pending_caller.strip()
            self._flush_caller_turn()
            if caller_text and capture_caller_name_answer(self.session, caller_text):
                self._caller_name_question_sent = False
                draft = get_draft(self.session)
                if (
                    draft.selected_slot
                    and draft.provider_verified
                    and self.session.booking_status not in {"booked", "rescheduled"}
                ):
                    self.session.booking_status = "awaiting_confirmation"
                    spoken = authoritative_availability_speech(self.session, self._tz)
                    if spoken:
                        await self._send_force_message(spoken)
                        self._mark_pending_booking_read_back(spoken)
                        self._after_availability_line = self._next_collection_line()
                        self._prompt_caller_name_after_response = (
                            self._after_availability_line is not None
                        )
                await self._persist_session()
                return
            # Once a provider-checked draft has been explicitly read back, an
            # unqualified caller affirmation commits it here. This makes the
            # final write independent of whether the realtime model remembers
            # to issue confirm_appointment on its next turn.
            if (
                caller_text
                and self._awaiting_wrap_up
                and _caller_is_finished(caller_text)
            ):
                if getattr(self, "_protect_playback", False):
                    self._deferred_wrap_up = True
                    await self._persist_session()
                    return
                self._awaiting_wrap_up = False
                await self._send_force_message(
                    "Perfect. We look forward to seeing you. Have a great day!"
                )
                await self._persist_session()
                return
            if caller_text and await self._confirm_pending_booking_from_caller(caller_text):
                await self._persist_session()
                return
            await self._persist_session()
            return

        if etype in _AGENT_TRANSCRIPT_DELTA:
            self._mark_timing("LLM FIRST TOKEN")
            # The caller's turn ended the moment the agent began replying.
            self._flush_caller_turn()
            delta = _first_str(data, "delta", "transcript", "text")
            if delta:
                candidate_text = self._pending_agent + delta
                if (
                    self._should_cancel_restart_greeting(event)
                    and self._looks_like_session_restart_greeting(candidate_text)
                ):
                    await self._suppress_restart_greeting(
                        candidate_text, reason="already_greeted"
                    )
                    return
                if (
                    not self._booking_is_persisted
                    and looks_like_unverified_booking_success(candidate_text)
                ):
                    await self._replace_blocked_confirmation_speech(candidate_text)
                    return
                if self._is_unauthorized_availability_claim(candidate_text):
                    await self._block_unverified_availability(candidate_text)
                    return
                self._pending_agent = candidate_text
            return

        if etype in _AGENT_TRANSCRIPT_DONE:
            self._flush_caller_turn()
            # A `.done` carrying the full text supersedes the accumulated
            # deltas; otherwise the deltas are all we have.
            text = _first_str(data, "transcript", "text")
            if text:
                if self._blocked_unverified_success:
                    self._blocked_unverified_success = False
                elif (
                    self._should_cancel_restart_greeting(event)
                    and self._looks_like_session_restart_greeting(text)
                ):
                    await self._suppress_restart_greeting(text, reason="already_greeted")
                elif (
                    not self._booking_is_persisted
                    and looks_like_unverified_booking_success(text)
                ):
                    await self._replace_blocked_confirmation_speech(text)
                elif self._is_unauthorized_availability_claim(text):
                    await self._block_unverified_availability(text)
                else:
                    self._pending_agent = text
            readback_text = self._pending_agent or text
            if readback_text:
                self._mark_pending_booking_read_back(readback_text)
            self._flush_agent_turn()
            await self._persist_session()
            return

        if etype in _FUNCTION_CALL_DONE:
            self._flush_caller_turn()
            await self._handle_function_call(data)
            return

        # Observed on a live socket: server errors can arrive as a bare
        # {"error": "..."} frame with no `type` at all, so keying only on
        # type == "error" would file real failures under _UNHANDLED.
        if etype == "error" or (not etype and "error" in event):
            message = _first_str(data, "message", "error", "code") or json.dumps(data)[:300]
            logger.error("call %s: xAI realtime error: %s", self.call_id, message)
            if self.session.greeting_sent:
                truth(
                    "CALL_GREETING_SUPPRESSED",
                    call_sid=self.call_id,
                    reason="already_greeted",
                    error=message[:120],
                )
            elif self._startup_greeting_open():
                truth(
                    "CALL_GREETING_FAILED",
                    call_sid=self.call_id,
                    reason="xai_error",
                    error=message[:120],
                )
                await self._greet_via_model()
            else:
                truth(
                    "CALL_GREETING_SUPPRESSED",
                    call_sid=self.call_id,
                    reason="already_greeted",
                    error=message[:120],
                )
            return

        if etype == "session.created":
            self._xai_session_ready.set()
            # The only observability there is on the voice setting: xAI accepts
            # any voice string without validating it and never echoes it back,
            # so a typo (or a name from another vendor's catalogue) silently
            # leaves the caller listening to the server default instead of the
            # configured persona. Log both so the mismatch is greppable.
            requested = resolve_xai_voice(
                self.session.entities.get("xai_voice") or settings.XAI_VOICE_ID
            )
            server_default = (event.get("session") or {}).get("voice")
            server_model = (event.get("session") or {}).get("model") or "server-selected"
            logger.info(
                "call %s: xAI session created voice=%s realtime_model=%s",
                self.call_id,
                requested,
                server_model,
            )
            if server_default and requested != server_default:
                logger.info(
                    "call %s: requested voice %r (server default %r). xAI does not "
                    "validate voice names — if the caller hears the wrong voice, "
                    "this is the first thing to check.",
                    self.call_id, requested, server_default,
                )
            return

        if etype in _BENIGN_EVENTS:
            if etype == "response.created":
                self._active_response_id = self._response_id_of(data) or "unknown"
                self._blocked_unverified_success = False
                if self._availability_expect_hold and self._active_response_id != self._availability_model_response_id:
                    self._availability_hold_response_id = self._active_response_id
                    self._availability_expect_hold = False
                if self._prompt_caller_name_after_response:
                    self._caller_name_prompt_after_id = self._active_response_id
                    self._prompt_caller_name_after_response = False
                if self._greeting_pending and not self.session.greeting_sent:
                    self._greeting_response_id = self._active_response_id
                    truth(
                        "CALL_GREETING_RESPONSE_CREATED",
                        call_sid=self.call_id,
                        response_id=self._greeting_response_id,
                    )
            elif etype == "response.done":
                self._mark_timing("LLM COMPLETE")
                self._flush_agent_turn()
                self._finish_turn()
                finished_response_id = self._active_response_id
                greeting_failed = (
                    self._greeting_pending
                    and not self.session.greeting_sent
                    and not self._greeting_got_audio
                    and (
                        not self._greeting_response_id
                        or finished_response_id == self._greeting_response_id
                        or self._response_id_of(data) == self._greeting_response_id
                    )
                )
                self._active_response_id = None
                finished = self._response_id_of(data) or finished_response_id
                if finished and finished == self._availability_hold_response_id:
                    self._availability_hold_done = True
                    pending_availability = self._pending_availability_speech
                    self._pending_availability_speech = None
                    if pending_availability and not self._availability_speech_interrupted:
                        await self._speak_availability(pending_availability)
                        return
                if finished and finished == self._caller_name_prompt_after_id:
                    self._caller_name_prompt_after_id = None
                    line = self._after_availability_line
                    self._after_availability_line = None
                    if line:
                        await self._speak_collection_line(line)
                        return
                if greeting_failed:
                    truth(
                        "CALL_GREETING_FAILED",
                        call_sid=self.call_id,
                        reason="no_audio",
                        response_id=self._greeting_response_id,
                    )
                    if self._startup_greeting_open():
                        await self._greet_via_model()
                    return
                if self._pending_forced_tool_message is not None:
                    forced_message = self._pending_forced_tool_message
                    self._pending_forced_tool_message = None
                    # Defensive: a deterministic terminal follow-up replaces any
                    # generic model continuation for the same tool result.
                    self._response_needed_after_tool = False
                    self._restricted_response_needed_after_tool = False
                    logger.info(
                        "call %s: response %s closed after authoritative tool result -> sending force_message",
                        self.call_id,
                        finished_response_id,
                    )
                    await self._send_force_message(
                        forced_message,
                        protect_playback="anything else I can help" in forced_message,
                    )
                elif self._restricted_response_needed_after_tool:
                    self._restricted_response_needed_after_tool = False
                    self._response_needed_after_tool = False
                    self._log_tool_continuation(
                        "issued", reason="restricted_followup_deferred",
                        closed_response_id=finished_response_id,
                    )
                    await self._send({"type": "response.create", "response": {"tool_choice": "none"}})
                elif self._response_needed_after_tool:
                    self._response_needed_after_tool = False
                    self._log_tool_continuation(
                        "issued", reason="tool_result_complete_deferred",
                        closed_response_id=finished_response_id,
                    )
                    await self._send({"type": "response.create"})
            return

        logger.debug("call %s: _UNHANDLED %s %s", self.call_id, etype, json.dumps(event)[:400])

    async def run(self) -> None:
        headers = {"Authorization": f"Bearer {settings.XAI_API_KEY}"}
        try:
            async with ws_connect(self._url, additional_headers=headers) as ws:
                self._ws = ws
                await self._configure()
                await self._await_xai_session_ready()
                await self._greet()
                logger.info("call %s: realtime session open", self.call_id)

                while True:
                    remaining = self._deadline - time.monotonic()
                    if remaining <= 0:
                        logger.warning("call %s: exceeded max call duration", self.call_id)
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        logger.warning("call %s: exceeded max call duration", self.call_id)
                        break
                    except websockets.exceptions.ConnectionClosed:
                        break

                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("call %s: non-JSON frame dropped", self.call_id)
                        continue
                    # Provider tools must not stall websocket receive / VAD / barge-in.
                    etype = event.get("type") if isinstance(event, dict) else None
                    if etype in _FUNCTION_CALL_DONE:
                        task = asyncio.create_task(self._dispatch(event))
                        self._inflight_tools.add(task)
                        task.add_done_callback(self._inflight_tools.discard)
                    else:
                        await self._dispatch(event)
        except Exception:
            logger.exception("call %s: realtime session failed", self.call_id)
        finally:
            self._flush_caller_turn()
            self._flush_agent_turn()
            await self._finalize()

    # -------------------------------------------------------------- teardown
    async def _finalize(self) -> None:
        """Persist transcript, summary and status — the <Gather> flow's
        /voice/status handler does the same job for the Twilio path."""
        self._cancel_greeting_watchdog()
        mark_call_ended(self.session)
        store = self._store()
        await store.save(self.session)
        await store.end(self.call_id)

        transcript = self.session.transcript_text
        analysis = None
        if self.session.history:
            analysis = await grok_service.analyze_call(self.session)

        try:
            async with AsyncSessionLocal() as db:
                call_log = (
                    await db.execute(
                        select(CallLog).where(
                            CallLog.twilio_call_sid == self._call_log_sid
                        )
                    )
                ).scalar_one_or_none()
                if call_log is None:
                    logger.warning("call %s: no CallLog row to finalize", self.call_id)
                    return
                call_log.status = CallStatus.COMPLETED
                call_log.ended_at = datetime.now(timezone.utc)
                if call_log.started_at:
                    call_log.duration_seconds = int(
                        (call_log.ended_at - call_log.started_at).total_seconds()
                    )
                call_log.transcript = transcript or None
                call_log.ai_analysis = {
                    **(call_log.ai_analysis or {}),
                    "booking_outcome": self.session.booking_status
                    if self.session.booking_status != "none"
                    else "no_booking",
                    "linked_appointment_id": self.session.appointment_id,
                }
                if analysis:
                    call_log.ai_summary = analysis.summary
                    call_log.ai_analysis.update(analysis.model_dump())
                    if call_log.direction is CallDirection.INBOUND:
                        call_log.primary_language = primary_caller_language(
                            self.session, analysis.primary_language
                        )
                        await persist_caller_identity(
                            db, self.session, analysis.caller_name, analysis.caller_email
                        )
                await db.commit()
            logger.info(
                "call %s: finalized (%d chars of transcript)", self.call_id, len(transcript)
            )
        except Exception:
            logger.exception("call %s: failed to persist call log", self.call_id)


async def drive_call(call_id: str, session: CallSession) -> None:
    """Entry point used as a FastAPI background task by the incoming webhook."""
    await XAIVoiceSession(call_id, session).run()
