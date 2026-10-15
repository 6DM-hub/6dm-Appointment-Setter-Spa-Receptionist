"""Protection for a booking that already exists on this call.

Shared by every voice path (realtime and the legacy text loop) and by every
business type. Nothing here knows about a particular industry, service menu,
provider or tenant: it works only with the call's booking draft, the
session's own entities and the caller's words.

Rules enforced here, in backend state rather than in prompts:

* Once a booking completes, any stale "offer"/"consent" state from it is
  cleared and the booked state is snapshotted, so a later bare "yes" can never
  confirm something the caller did not just agree to.
* "Keep it" / "never mind" / "leave it as it is" while a change, an
  additional booking or a clarification is pending restores the booked state
  exactly, withdraws every pending proposal and consent, and books nothing.
* Whether a later request changes the booking or adds another is decided from
  the caller's words; unclear requests are asked about first.
* An additional appointment is only confirmable after the caller has been
  told it IS additional, and has answered on a later turn.

All state lives in ``session.entities`` (JSON-serialisable) so it survives a
reconnect, when the realtime session object is rebuilt from Redis.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from app.services.booking_state import (
    BOOKING_CHANGE_CLARIFICATION,
    BOOKING_CHANGE_IN_PROGRESS,
    BookingDraft,
    _parse_provider_iso,
    additional_booking_notice,
    booking_change_question,
    classify_change_question_answer,
    classify_post_booking_request,
    consume_confirmation_authorization,
    get_draft,
    invalidate_booking_proposal,
    kept_booking_speech,
    save_draft,
)
from app.services.truth_log import truth

# Session entities ----------------------------------------------------------
BOOKING_COMPLETED = "booking_completed"            # {"appointment_id", "turn"}
BOOKED_DRAFT_SNAPSHOT = "booked_draft_snapshot"    # {"appointment_id", "turn", "draft", mirrors}
ADDITIONAL_BOOKING = "additional_booking_pending"  # {"booking_id", "notice", "notice_spoken", "notice_turn"}
USER_TURN_COUNT = "realtime_user_turn_count"       # persisted caller-turn counter
BOOKING_KEPT_TURN = "booking_kept_turn"            # caller turn of the last "keep it"

# Consent/offer state that belongs to ONE proposal. Never carried past a
# completed booking or a "keep it".
_STALE_OFFER_KEYS = (
    "accepted_booking_offer",
    "spoken_booking_offer",
    "spoken_booking_choices",
    "enhancement_base_time_accepted",
    "caller_confirmed_revision",
    "caller_confirmed_fingerprint",
)


# --------------------------------------------------------------------------- #
# Booking completion and the booked snapshot
# --------------------------------------------------------------------------- #
def current_turn(session: Any) -> int:
    """The caller's turn number, the same on every voice path.

    The realtime path keeps a persisted counter; the legacy text loop counts
    the caller turns in the call history.
    """
    stored = session.entities.get(USER_TURN_COUNT)
    if stored is not None:
        return int(stored)
    return sum(1 for item in (getattr(session, "history", None) or []) if item.get("role") == "user")


def last_caller_utterance(session: Any) -> str:
    for item in reversed(getattr(session, "history", None) or []):
        if item.get("role") == "user":
            return str(item.get("content") or "")
    return ""


def record_booking_completed(session: Any, turn: int) -> None:
    """A booking or reschedule just completed on this call."""
    draft = get_draft(session)
    session.entities[BOOKING_COMPLETED] = {
        "appointment_id": draft.appointment_id,
        "turn": int(turn),
        "history_len": len(getattr(session, "history", None) or []),
    }
    session.entities[BOOKED_DRAFT_SNAPSHOT] = {
        "appointment_id": draft.appointment_id,
        "turn": int(turn),
        "draft": draft.to_dict(),
        "selected_service": getattr(session, "selected_service", None),
        "selected_duration": getattr(session, "selected_duration", None),
        "confirmed_datetime": getattr(session, "confirmed_datetime", None),
        "requested_datetime": getattr(session, "requested_datetime", None),
    }
    session.entities.pop(BOOKING_KEPT_TURN, None)
    for key in _STALE_OFFER_KEYS:
        session.entities.pop(key, None)
    for key in (BOOKING_CHANGE_CLARIFICATION, BOOKING_CHANGE_IN_PROGRESS, ADDITIONAL_BOOKING):
        session.entities.pop(key, None)


def completed_turn(session: Any) -> int | None:
    marker = session.entities.get(BOOKING_COMPLETED) or {}
    turn = marker.get("turn")
    return int(turn) if turn is not None else None


def caller_spoke_since_booking(session: Any) -> bool:
    """The caller has said something after the last booking completed."""
    marker = session.entities.get(BOOKING_COMPLETED) or {}
    if "history_len" not in marker:
        return False
    history = getattr(session, "history", None) or []
    return any(item.get("role") == "user" for item in history[int(marker["history_len"]):])


def booking_on_record(session: Any) -> bool:
    """Some booking completed earlier in this call."""
    return completed_turn(session) is not None


def after_completed_booking(session: Any, turn: int) -> bool:
    """The active draft IS a completed booking and this is a later turn."""
    done = completed_turn(session)
    return bool(get_draft(session).is_persisted and done is not None and turn > done)


def booked_details(session: Any) -> tuple[str | None, str | None]:
    """(service, start ISO) of the last booking the calendar holds, not the draft."""
    from app.services.appointment_booking_service import BOOKED_SERVICE_IDENTITIES

    snapshot = session.entities.get(BOOKED_DRAFT_SNAPSHOT) or {}
    booked = BookingDraft.from_dict(snapshot.get("draft")) if snapshot.get("draft") else get_draft(session)
    # What the calendar provider confirmed for this appointment, when known.
    identity = (session.entities.get(BOOKED_SERVICE_IDENTITIES) or {}).get(str(booked.appointment_id)) or {}
    service = identity.get("title") or booked.service_description
    iso = (
        identity.get("start")
        or (booked.selected_slot or {}).get("start")
        or snapshot.get("confirmed_datetime")
        or booked.start_iso
    )
    return service, iso


def change_in_progress(session: Any) -> bool:
    marker = session.entities.get(BOOKING_CHANGE_IN_PROGRESS) or {}
    done = completed_turn(session)
    return bool(
        marker
        and marker.get("appointment_id") == get_draft(session).appointment_id
        and int(marker.get("turn", -1)) > int(-1 if done is None else done)
    )


def additional_marker(session: Any) -> dict[str, Any] | None:
    marker = session.entities.get(ADDITIONAL_BOOKING) or None
    draft = get_draft(session)
    if not marker or marker.get("booking_id") != draft.booking_id or draft.is_persisted:
        return None
    return marker


def _draft_differs_from_booking(session: Any) -> bool:
    snapshot = session.entities.get(BOOKED_DRAFT_SNAPSHOT) or {}
    if not snapshot.get("draft"):
        return False
    booked = BookingDraft.from_dict(snapshot["draft"])
    current = get_draft(session)
    return (
        current.booking_id != booked.booking_id
        or current.start_iso != booked.start_iso
        or current.end_iso != booked.end_iso
        or current.service_description != booked.service_description
        or current.operation_mode != booked.operation_mode
    )


def something_pending(session: Any) -> bool:
    """A change, an additional booking or a change/add question is open."""
    return bool(
        change_in_progress(session)
        or session.entities.get(BOOKING_CHANGE_CLARIFICATION)
        or additional_marker(session)
        or _draft_differs_from_booking(session)
    )


# --------------------------------------------------------------------------- #
# "Keep it"
# --------------------------------------------------------------------------- #
_CLOCK_RE = re.compile(
    r"\b(?P<h>[01]?\d|2[0-3])(?::(?P<m>[0-5]\d))?\s*(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?|o'?clock)?\b",
    re.IGNORECASE,
)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _mentions_other_time(utterance: str, booked_iso: str | None, tz: Any) -> bool:
    """"Keep it at 3" when the booking is at 2 is a change, not a keep."""
    parsed = _parse_provider_iso(booked_iso)
    if parsed is None:
        return False
    local: datetime = parsed.astimezone(tz) if parsed.tzinfo else parsed
    text = (utterance or "").casefold()
    for day in _WEEKDAYS:
        if re.search(rf"\b{day}\b", text) and day != local.strftime("%A").casefold():
            return True
    for match in _CLOCK_RE.finditer(text):
        hour, minute = int(match.group("h")), int(match.group("m") or 0)
        marker = (match.group("ap") or "").replace(".", "").replace(" ", "")
        if not marker and not match.group("m") and hour > 12:
            continue  # a bare number such as "60" is not a time
        if marker.startswith("p") and hour < 12:
            hour += 12
        elif marker.startswith("a") and hour == 12:
            hour = 0
        same = (hour, minute) == (local.hour, local.minute) if marker.startswith(("a", "p")) else (
            hour % 12 == local.hour % 12 and minute == local.minute
        )
        if not same:
            return True
    return False


def keep_request(session: Any, utterance: str | None, tz: Any) -> bool:
    """The caller wants the booking left exactly as it is."""
    if classify_change_question_answer(utterance) != "keep":
        return False
    _service, booked_iso = booked_details(session)
    return not _mentions_other_time(utterance or "", booked_iso, tz)


def kept_this_turn(session: Any, turn: int) -> bool:
    kept = session.entities.get(BOOKING_KEPT_TURN)
    return kept is not None and int(kept) == int(turn)


def keep_existing_booking(session: Any, tz: Any, *, turn: int, call_sid: str | None = None) -> str:
    """Restore the booked state exactly; withdraw every pending proposal.

    Returns the line to say. Writes nothing to any calendar.
    """
    service, booked_iso = booked_details(session)
    snapshot = session.entities.get(BOOKED_DRAFT_SNAPSHOT) or {}
    for key in (BOOKING_CHANGE_CLARIFICATION, BOOKING_CHANGE_IN_PROGRESS, ADDITIONAL_BOOKING):
        session.entities.pop(key, None)
    for key in _STALE_OFFER_KEYS:
        session.entities.pop(key, None)
    if snapshot.get("draft"):
        restored = BookingDraft.from_dict(snapshot["draft"])
        # The restored booking is not awaiting any consent.
        restored.read_back = False
        restored.confirmation_authorized = False
        save_draft(session, restored)
        session.selected_service = snapshot.get("selected_service") or restored.service_description
        session.selected_duration = snapshot.get("selected_duration")
        if restored.appointment_id:
            session.appointment_id = restored.appointment_id
            session.external_booking_id = restored.external_booking_id
        if snapshot.get("confirmed_datetime"):
            session.confirmed_datetime = snapshot["confirmed_datetime"]
        if snapshot.get("requested_datetime"):
            session.requested_datetime = snapshot["requested_datetime"]
    else:
        invalidate_booking_proposal(session, "caller_kept_existing_booking")
    consume_confirmation_authorization(session)
    session.booking_status = "booked"
    session.entities[BOOKING_KEPT_TURN] = int(turn)
    truth("BOOKING_KEPT_AS_IS", call_sid=call_sid, appointment_id=get_draft(session).appointment_id)
    return kept_booking_speech(service, booked_iso, tz)


# --------------------------------------------------------------------------- #
# Change, add, keep or ask
# --------------------------------------------------------------------------- #
def _same_start(left: str | None, right: str | None, tz: Any) -> bool:
    from app.services.appointment_booking_service import _parse_dt

    a, b = _parse_dt(left, tz), _parse_dt(right, tz)
    return a is not None and b is not None and a == b


def proposal_repeats_booking(session: Any, args: dict[str, Any], tz: Any) -> bool:
    """The proposal restates what is booked, with nothing new."""
    from app.services.appointment_booking_service import _same_service_family

    if change_in_progress(session) or args.get("guest_name") or args.get("earliest"):
        return False
    current = get_draft(session)
    if args.get("preferred_staff") and args["preferred_staff"] != current.preferred_staff:
        return False
    service, booked_iso = booked_details(session)
    requested = args.get("requested_start_iso")
    if not requested or not booked_iso or not _same_start(requested, booked_iso, tz):
        return False
    wanted = str(" + ".join(args.get("requested_services") or []) or args.get("service_description") or "")
    return not wanted or _same_service_family(wanted, service)


def decide(
    session: Any,
    utterance: str | None,
    *,
    model_operation: str | None,
    appointment_id: str | None,
    args: dict[str, Any],
    turn: int,
    tz: Any,
    call_sid: str | None = None,
) -> str:
    """keep | modify | additional | ask | wait | unchanged, after a booking.

    The caller's words decide; the model's label never overrules them. Where
    the two disagree, or the words are unclear, the answer is "ask".
    """
    pending = session.entities.get(BOOKING_CHANGE_CLARIFICATION) or None
    if pending and int(pending.get("turn", -1)) == int(turn):
        return "wait"  # already asked this turn; do not ask twice
    if keep_request(session, utterance, tz):
        truth("BOOKING_CHANGE_INTENT", call_sid=call_sid, decision="keep",
              model_operation=model_operation or "none")
        return "keep"
    current = get_draft(session)
    if pending:
        kind = classify_change_question_answer(utterance)
        if kind in {"ambiguous", "keep"}:
            kind = classify_post_booking_request(utterance)
            if kind == "maybe_modify":
                kind = "ambiguous"
    else:
        kind = classify_post_booking_request(utterance)
        if kind != "additional" and proposal_repeats_booking(session, args, tz):
            return "unchanged"
        if change_in_progress(session) and kind != "additional":
            kind = "modify"
        elif appointment_id and appointment_id in (current.offered_appointment_ids or []):
            kind = "modify" if kind != "additional" else "ambiguous"
        elif kind == "maybe_modify":
            kind = "modify" if model_operation == "reschedule" else "ambiguous"
        elif kind == "modify" and model_operation == "schedule":
            kind = "ambiguous"
        elif kind == "additional" and model_operation == "reschedule":
            kind = "ambiguous"
    if kind == "ambiguous":
        attempts = int((pending or {}).get("attempts", 0)) + 1
        session.entities[BOOKING_CHANGE_CLARIFICATION] = {
            "turn": int(turn),
            "appointment_id": current.appointment_id,
            "attempts": attempts,
        }
        truth("BOOKING_CHANGE_CLARIFICATION_ASKED", call_sid=call_sid,
              model_operation=model_operation or "none", attempt=attempts)
        return "ask"
    session.entities.pop(BOOKING_CHANGE_CLARIFICATION, None)
    if kind == "modify":
        session.entities[BOOKING_CHANGE_IN_PROGRESS] = {
            "turn": int(turn),
            "appointment_id": current.appointment_id,
        }
    else:
        session.entities.pop(BOOKING_CHANGE_IN_PROGRESS, None)
    truth("BOOKING_CHANGE_INTENT", call_sid=call_sid, decision=kind,
          model_operation=model_operation or "none")
    return kind


def change_question(session: Any, tz: Any) -> str:
    service, booked_iso = booked_details(session)
    attempt = int((session.entities.get(BOOKING_CHANGE_CLARIFICATION) or {}).get("attempts", 1))
    return booking_change_question(service, booked_iso, tz, attempt=attempt)


# --------------------------------------------------------------------------- #
# Additional appointments
# --------------------------------------------------------------------------- #
def mark_additional_booking(session: Any, tz: Any) -> None:
    """The active (new) intent is an ADDITIONAL appointment."""
    service, booked_iso = booked_details(session)
    session.entities[ADDITIONAL_BOOKING] = {
        "booking_id": get_draft(session).booking_id,
        "notice": additional_booking_notice(service, booked_iso, tz),
        "notice_spoken": False,
        "notice_turn": None,
    }


def note_additional_notice_spoken(session: Any, turn: int) -> None:
    marker = additional_marker(session)
    if marker is not None:
        marker["notice_spoken"] = True
        marker["notice_turn"] = int(turn)


def additional_confirm_gate(session: Any, turn: int) -> tuple[str, str | None] | None:
    """None if confirming may proceed; else ("speak", notice) or ("wait", None)."""
    marker = additional_marker(session)
    if marker is None:
        return None
    if not marker.get("notice_spoken"):
        note_additional_notice_spoken(session, turn)
        return "speak", f"{marker['notice']} Would you like me to book it?"
    if int(turn) <= int(marker.get("notice_turn") if marker.get("notice_turn") is not None else -1):
        return "wait", None
    return None


# --------------------------------------------------------------------------- #
# Cancellation needs the caller's own, current words
# --------------------------------------------------------------------------- #
_CANCEL_RE = re.compile(
    r"\b(?:cancel\w*|call (?:it|that|the appointment|my appointment) off|"
    r"anular\w*)\b",
    re.IGNORECASE,
)
_NEGATED_CANCEL_RE = re.compile(
    r"\b(?:don'?t|do not|no need to|not|never|no,? don'?t|without|stop|"
    r"wouldn'?t|won'?t|no quiero|no)\s+(?:\w+\s+){0,2}(?:cancel\w*|anular\w*)",
    re.IGNORECASE,
)
_CANCEL_LOOKBACK_TURNS = 3


def cancel_request(utterance: str | None) -> bool:
    """The caller asks, in these words, for an appointment to be cancelled."""
    text = (utterance or "").replace("’", "'")
    return bool(_CANCEL_RE.search(text)) and not _NEGATED_CANCEL_RE.search(text)


def _withdraws_cancellation(utterance: str | None) -> bool:
    from app.services.booking_state import is_negative

    text = (utterance or "").replace("’", "'")
    return bool(
        _NEGATED_CANCEL_RE.search(text)
        or classify_change_question_answer(text) == "keep"
        or is_negative(text)
    )


def cancellation_authorization(session: Any) -> str:
    """allowed | kept | unconfirmed, from the caller's own words.

    * "kept": the caller's latest words keep the appointment ("keep it",
      "never mind", "don't cancel", "no") -- nothing may be cancelled.
    * "allowed": the latest caller turn asks to cancel, or is a plain yes that
      follows the caller's own cancellation request (within a few turns, with
      no "keep it" in between).
    * "unconfirmed": anything else. A model's decision to cancel is never
      enough on its own.
    A session with no caller speech at all (not a phone conversation) is not
    gated here.
    """
    from app.services.booking_state import is_affirmative

    caller_turns = [
        str(item.get("content") or "")
        for item in (getattr(session, "history", None) or [])
        if item.get("role") == "user"
    ]
    if not caller_turns:
        return "allowed"
    latest = caller_turns[-1]
    if _withdraws_cancellation(latest):
        return "kept"
    if cancel_request(latest):
        return "allowed"
    if is_affirmative(latest):
        for earlier in reversed(caller_turns[-1 - _CANCEL_LOOKBACK_TURNS:-1]):
            if _withdraws_cancellation(earlier):
                return "unconfirmed"
            if cancel_request(earlier):
                return "allowed"
    return "unconfirmed"


def refused_cancellation_speech(session: Any, tz: Any) -> str:
    """What the caller hears when a cancellation they did not ask for is refused."""
    if booking_on_record(session):
        service, booked_iso = booked_details(session)
        return kept_booking_speech(service, booked_iso, tz).replace(
            "and nothing new was booked", "and nothing was cancelled"
        )
    return (
        "No problem, I haven't cancelled anything. Your appointment stays as it is. "
        "Is there anything else I can help you with?"
    )
