"""One active booking intent per call: draft -> confirmed -> persisted.

The duplicate-appointment bug this module exists to kill came from treating
*intent detection* as the trigger to write. The agent booked the moment a date
was heard, so "September 20" created an appointment and "actually, make it the
22nd" created a second one, after which the final confirmation tripped over the
first and told the caller the slot was already taken.

The model here separates three things that were previously conflated:

  draft      the caller's current wish. Mutable, overwritten in place every
             time they change their mind, and never written to a calendar.
  intent     one booking the caller is trying to make. Identified by
             `booking_id`, which survives any number of draft edits and only
             rotates when the caller explicitly asks for a *separate*
             appointment.
  persisted  the appointment row and provider event backing an intent. At most
             one per intent, enforced by a unique index on
             `appointments.booking_intent_key`.

So changing the date edits the draft (before persistence) or moves the existing
event (after it). Neither path can produce a second appointment, because the
key that identifies the row is derived from the intent, not from the date.

Confirmation is deliberately decided here rather than in a prompt: a model told
"don't book twice" still books twice. `is_affirmative` gives the state machine
a caller-side signal it can check itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.services.truth_log import truth

# Matched against the caller's most recent utterance only. Kept deliberately
# tight: a false positive books an appointment the caller never agreed to,
# which is far worse than asking them to confirm twice.
_ENGLISH_AFFIRMATIVE = {
    "yes", "yeah", "yep", "yup", "yes please", "sure", "ok", "okay", "correct",
    "right", "exactly", "perfect", "great", "sounds good", "that works",
    "works for me", "go ahead", "book it", "confirm", "confirmed", "do it",
    "lets do it", "let's do it", "please do", "absolutely", "definitely",
    "that's right", "thats right", "affirmative", "sounds great", "i confirm",
}

# Exact-only confirmations in the languages supported by Grok Realtime. They
# are intentionally not eligible for the English prefix rule below: "sí" may
# confirm, while "sí, pero mañana" must never book the prior slot.
_TRANSLATED_AFFIRMATIVE = {
    "sí", "sí por favor", "si por favor", "de acuerdo", "adelante",  # Spanish
    "oui", "bien sûr", "d'accord", "allez y",  # French
    "sim", "pode reservar", "está certo",  # Portuguese
    "ja", "natürlich", "in ordnung",  # German
    "sì", "certo", "va bene", "proceda",  # Italian
    "ya", "iya", "tentu", "silakan",  # Indonesian
    "evet", "tabii", "devam",  # Turkish
    "vâng", "đúng", "được",  # Vietnamese
    "да", "конечно",  # Russian
    "نعم", "بالتأكيد",  # Arabic
    "हाँ", "हां", "बिल्कुल",  # Hindi
    "হ্যাঁ",  # Bengali
    "是", "好的", "可以",  # Chinese
    "はい", "お願いします",  # Japanese
    "네", "예", "좋아요",  # Korean
}
_AFFIRMATIVE = _ENGLISH_AFFIRMATIVE | _TRANSLATED_AFFIRMATIVE

_NEGATIVE = {
    "no", "no thanks", "no thank you", "not now",
    "no gracias", "ahora no",  # Spanish
    "non", "non merci", "pas maintenant",  # French
    "não", "não obrigado", "agora não",  # Portuguese
    "nein", "nein danke", "jetzt nicht",  # German
    "no grazie", "non ora",  # Italian
    "tidak", "tidak terima kasih",  # Indonesian
    "hayır", "hayır teşekkürler",  # Turkish
    "không", "không cảm ơn",  # Vietnamese
    "нет", "нет спасибо",  # Russian
    "لا", "لا شكرا",  # Arabic
    "नहीं",  # Hindi
    "না",  # Bengali
    "不", "不用", "不要",  # Chinese
    "いいえ", "結構です",  # Japanese
    "아니요", "괜찮아요",  # Korean
}

# Any of these anywhere in the utterance means the caller is still negotiating,
# and vetoes an affirmative match: "yes, but can we do Tuesday instead?" is a
# change request, not a confirmation.
_VETO = re.compile(
    r"\b(no|nope|not|don't|dont|instead|actually|change|different|another|"
    r"rather|wait|hold on|cancel|reschedule|move|but|"
    # Sign-offs: "great, bye" is the caller leaving, not agreeing to a booking.
    r"bye|goodbye|later|nevermind|never mind)\b",
    re.IGNORECASE,
)

_PUNCT = re.compile(r"[^\w\s']+")
_WS = re.compile(r"\s+")

# Leftover tokens after stripping a known agreement phrase that are still just
# courtesy / confirmation wording — not a change to service, date, time, or staff.
_CONFIRMATION_FILLER = {
    "please", "thanks", "thank", "you", "yeah", "yes", "yep", "yup", "ok", "okay",
    "sure", "right", "correct", "exactly", "perfect", "great", "sounds", "good",
    "that", "works", "for", "me", "go", "ahead", "book", "it", "confirm", "confirmed",
    "do", "lets", "let's", "absolutely", "definitely", "thats", "that's", "affirmative",
    "i", "the", "a", "and", "just", "fine", "alright", "all", "set", "too", "also",
    "okey", "yess", "yesss",
}

# Any of these in the utterance means the caller is still specifying a booking
# field, even if they also said "yes".
_BOOKING_FIELD_MODIFICATION = re.compile(
    r"\b("
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|june|july|august|september|october|"
    r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|"
    r"today|tomorrow|tonight|morning|afternoon|evening|"
    r"massage|facial|hydrolux|swedish|treatment|manicure|pedicure|wrap|"
    r"package|service|session|"
    r"am|pm|o'?clock"
    r")\b"
    r"|with\s+[a-z]"
    r"|\d{1,2}(:\d{2})?",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", (text or "").casefold())).strip()


_CALLER_INTRO_RE = re.compile(
    r"\b(?:my name is|this is|i am|i['’]m)\s+(?P<name>[A-Za-z][A-Za-z'\-]+)",
    re.IGNORECASE,
)
_STAFF_REQUEST_RE = re.compile(
    r"\b(?:book(?:ing)? with|see|prefer|with (?:my )?(?:therapist|technician|esthetician)?\s*)"
    r"(?P<name>[A-Za-z][A-Za-z'\-]+)"
    r"|"
    r"\b(?:therapist|technician|esthetician)\s+(?P<name2>[A-Za-z][A-Za-z'\-]+)",
    re.IGNORECASE,
)
_GUEST_RE = re.compile(
    r"\b(?:for|booking (?:this )?for)\s+(?P<name>[A-Za-z][A-Za-z'\-]+)",
    re.IGNORECASE,
)


def _last_user_text(session: Any) -> str:
    history = list(getattr(session, "history", None) or [])
    for turn in reversed(history):
        if turn.get("role") == "user":
            return str(turn.get("content") or "")
    return ""


def _same_utterance_has_caller_and_staff(session: Any) -> bool:
    """True when one caller turn both identifies the caller and requests staff."""
    text = _last_user_text(session)
    return bool(_CALLER_INTRO_RE.search(text) and _STAFF_REQUEST_RE.search(text))


_CALLER_NAME_QUESTION = re.compile(
    r"\b(?:your full name|your name|full name for (?:the|your) appointment|"
    r"name for (?:the|your) appointment|name on (?:the|your) appointment)\b",
    re.IGNORECASE,
)


def _was_answer_to_caller_name_question(session: Any, value: str | None) -> bool:
    """True when `value` was supplied as the caller's identity, not staff preference.

    This is a defensive guard against the voice model copying an answer such as
    "Green" into both caller_name and preferred_staff after asking
    "May I have your full name for the appointment?"
    """
    target = _normalize(value or "")
    if not target:
        return False

    history = list(getattr(session, "history", None) or [])
    for idx in range(len(history) - 1, -1, -1):
        turn = history[idx]
        if turn.get("role") != "user":
            continue

        utterance = _normalize(str(turn.get("content") or ""))
        identity_forms = {
            target,
            f"my name is {target}",
            f"its {target}",
            f"this is {target}",
        }
        if utterance not in identity_forms:
            continue

        # Find the immediately preceding assistant turn, ignoring system notes.
        for previous in range(idx - 1, -1, -1):
            prior = history[previous]
            if prior.get("role") == "assistant":
                return bool(
                    _CALLER_NAME_QUESTION.search(str(prior.get("content") or ""))
                )
            if prior.get("role") == "user":
                break
        return False

    return False


def utterance_modifies_booking(utterance: str | None) -> bool:
    """True when the caller is still changing a booking-defining field.

    Modification always wins over affirmation: "yes, but make it Swedish" and
    "yeah, Friday" are edits, not consent to the previous proposal.
    """
    text = _normalize(utterance or "")
    if not text:
        return False
    if _VETO.search(text):
        return True
    remainder = text
    for phrase in sorted(_ENGLISH_AFFIRMATIVE, key=len, reverse=True):
        if remainder == phrase:
            return False
        if remainder.startswith(phrase + " "):
            remainder = remainder[len(phrase):].strip()
            break
    leftover = [word for word in remainder.split() if word and word not in _CONFIRMATION_FILLER]
    if not leftover:
        return False
    return bool(_BOOKING_FIELD_MODIFICATION.search(" ".join(leftover)))


def is_affirmative(utterance: str | None) -> bool:
    """Whether the caller plainly agreed to what was just read back to them.

    Requires a pure agreement with no booking-field modification. "Yes" and
    "yes please" confirm; "yes, but make it Tuesday" and "yeah, Friday" do not.
    """
    text = _normalize(utterance or "")
    if not text:
        return False
    if utterance_modifies_booking(text):
        return False
    if text in _AFFIRMATIVE:
        return True
    # "Yes" followed by anything other than courtesy or consent wording is not
    # agreement to the booking: "yes, where are you located?", "yes, I can
    # hear you" and "yes, Deep Cleaning" all say something else. Only the
    # words that cannot change or redirect the decision may follow.
    for phrase in sorted(_ENGLISH_AFFIRMATIVE, key=len, reverse=True):
        if text.startswith(phrase + " "):
            leftover = text[len(phrase):].split()
            return bool(leftover) and len(leftover) <= 8 and all(
                word in _CONSENT_FOLLOW_UP for word in leftover
            )
    return False


# Words that may follow "yes" without changing what the caller agreed to.
_CONSENT_FOLLOW_UP = _CONFIRMATION_FILLER | {
    "so", "much", "lovely", "wonderful", "awesome", "cool", "dear", "sir",
    "maam", "ma'am", "please", "thankyou", "very", "is", "would", "be",
    "sounds", "great", "good", "perfect", "ok", "okay", "yes",
    "that'd", "thatd", "i'd", "id", "love", "like", "to", "appreciate", "that's",
}

# A question CARA asks about the booking itself. A plain "yes" answers the
# booking only when CARA's last question was one of these.
_BOOKING_QUESTION_RE = re.compile(
    r"\b(?:book|booking|reserve|confirm|schedule|move|reschedul\w*|appointment|"
    r"go ahead|lock (?:it|that) in|work(?:s)? for you|sound(?:s)? good|"
    r"shall i|should i|want me to|like me to|that time|this time|"
    r"is that (?:right|correct))\b",
    re.IGNORECASE,
)
_LOOKS_LIKE_QUESTION_RE = re.compile(
    r"(?:^|[.!,;:]\s+)(?:would|should|shall|do|does|did|can|could|will|is|are|"
    r"may|have|has|what|where|when|which|who|how|want)\b",
    re.IGNORECASE,
)
# Topics that make a question about something OTHER than the booking, even
# when it also uses booking words ("want me to text you our address").
_OTHER_TOPIC_RE = re.compile(
    r"\b(?:address|directions|parking|located|location|text you|email you|send you|"
    r"link|website|policy|policies|price|prices|cost|fee|fees|hours|menu|gift)\b",
    re.IGNORECASE,
)
_REFUSAL_START_RE = re.compile(r"^(?:no|nope|nah|not|don'?t|do not|never)\b")


def answering_unrelated_question(session: Any) -> bool:
    """CARA's most recent question before this caller turn was not about the booking.

    "Would you like our address?" -> "Yes." is an answer to the address
    question, never consent to an earlier booking offer.
    """
    history = list(getattr(session, "history", None) or [])
    seen_caller = False
    for turn in reversed(history):
        role = turn.get("role")
        if role == "user":
            if seen_caller:
                return False
            seen_caller = True
            continue
        if role == "assistant" and seen_caller:
            text = str(turn.get("content") or "").strip()
            # A transcript may drop the "?": a line that opens like a question
            # is treated as one (fail closed).
            if not text.endswith("?") and not _LOOKS_LIKE_QUESTION_RE.search(text):
                return False
            return bool(_OTHER_TOPIC_RE.search(text)) or not _BOOKING_QUESTION_RE.search(text)
    return False


# ---------------------------------------------------------------------------
# A yes counts only as the answer to the question it follows
# ---------------------------------------------------------------------------
# The booking question CARA actually asked for the CURRENT proposal, and where
# in the conversation it was asked. A caller's yes authorizes that proposal
# only when it is the very next caller turn after that question and nothing
# else was asked in between (CARA's own backend-spoken lines excepted).
OPEN_BOOKING_QUESTION = "open_booking_question"
BACKEND_PROMPTS = "backend_prompts"   # lines CARA's backend itself spoke (recent)


def note_booking_question(session: Any, text: str | None = None) -> None:
    draft = get_draft(session)
    if not draft.selected_slot:
        return
    session.entities[OPEN_BOOKING_QUESTION] = {
        "history_len": len(getattr(session, "history", None) or []),
        "fingerprint": proposal_fingerprint(draft),
        "text": text,
        # "...Would you like me to book that? Would you like our address?" is
        # not a booking question: a yes to it may answer the other question.
        "ambiguous": bool(text) and _asks_something_else(session, text),
    }


_SENTENCE_RE = re.compile(r"[^.?!¿¡]+[.?!]?")
# Ways a spoken question opens. Counted ANYWHERE in the line, so neither a
# missing "?" nor joining two questions with "and" hides the second one.
_QUESTION_OPENER_RE = re.compile(
    r"\b(?:would you|wouldn'?t you|will you|shall i|shall we|should i|should we|"
    r"do you|did you|does that|does this|does it|can i|can we|can you|could i|could you|"
    r"may i|is that|is this|is it|are you|have you|how about|what about)\b",
    re.IGNORECASE,
)


def _asks_something_else(session: Any, text: str) -> bool:
    """A model-worded line that asks MORE than one question.

    Then a yes may answer any of them, so it is not consent to the booking
    (fail closed: "Does that work? Shall I book it?" costs one re-ask; the
    realtime re-ask is the backend's own single-question read-back). Lines
    the backend itself authored since the caller last spoke (its read-back,
    possibly translated) are its own wording and are not second-guessed.
    """
    history = list(getattr(session, "history", None) or [])
    since_caller = max((i + 1 for i, item in enumerate(history) if item.get("role") == "user"), default=0)
    if any(record["at"] >= since_caller and same_question(text, record["text"])
           for record in _prompt_records(session.entities.get(BACKEND_PROMPTS))):
        return False
    if len(_QUESTION_OPENER_RE.findall(text)) >= 2:
        return True
    questions = [s.strip() for s in _SENTENCE_RE.findall(text) if _is_question_line(s.strip())]
    return len(questions) >= 2


def note_backend_prompt(session: Any, text: str | None) -> None:
    """Record ONE occurrence of a line the backend itself is about to speak.

    Each record carries where in the conversation it was spoken. It vouches
    only for an assistant turn at or after that point, only inside the window
    of the question being answered, and only once: an old backend line that
    the model repeats later carries no authority.
    """
    if not text:
        return
    prompts = list(session.entities.get(BACKEND_PROMPTS) or [])
    prompts.append({"text": str(text)[:600], "at": len(getattr(session, "history", None) or [])})
    session.entities[BACKEND_PROMPTS] = prompts[-12:]


def _prompt_records(trusted: list | None) -> list[dict]:
    records = []
    for item in trusted or []:
        if isinstance(item, dict) and item.get("text"):
            records.append({"text": str(item["text"]), "at": int(item.get("at", -1))})
        elif isinstance(item, str):
            records.append({"text": item, "at": -1})   # legacy entry: no occurrence, no trust
    return records


def _plain_words(text: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", (text or "").casefold()).split())


def same_question(said: str | None, asked: str | None) -> bool:
    """An assistant turn is exactly (the transcript of) a line CARA itself spoke.

    Exact after normalising case and punctuation only. A turn that merely
    CONTAINS the trusted line ("... book that? Would you like our address
    before deciding?") is a different turn: its yes is ambiguous.
    """
    a, b = _plain_words(said), _plain_words(asked)
    return bool(a and b and a == b)


def _trailing_non_dialogue(tail: list) -> int:
    count = 0
    for item in reversed(tail):
        if item.get("role") in {"system", "tool"}:
            count += 1
            continue
        break
    return count


def answers_backend_question(
    session: Any, *, history_len: int, question: str | None, trusted: list | None = None,
    trust_backend_questions: bool = True,
) -> bool | None:
    """Does the latest caller turn answer the question asked at ``history_len``,
    with no other question asked since?

    None when no caller turn has happened since (a programmatic check, not a
    caller answer). Any assistant turn since then that asks something else
    ("would you like our address?") means the yes may answer that instead.
    Earlier caller turns in between are fine: one that changed or refused the
    proposal has already replaced or withdrawn it.
    """
    start = int(history_len)
    tail = list((getattr(session, "history", None) or [])[start:])
    users = [i for i, item in enumerate(tail) if item.get("role") == "user"]
    if not users:
        return None
    if users[-1] != len(tail) - 1 - _trailing_non_dialogue(tail):
        return False
    records = _prompt_records(trusted)
    used: set[int] = set()
    for offset, item in enumerate(tail[: users[-1]]):
        if item.get("role") != "assistant":
            continue
        said = str(item.get("content") or "")
        if not _plain_words(said):
            continue
        # Fail closed: punctuation in a voice transcript is not a consent
        # boundary, so ANY assistant line that is not exactly the question
        # itself or another line the backend spoke unbinds the yes.
        if question and same_question(said, question):
            continue
        position = start + offset
        voucher = next((
            n for n, record in enumerate(records)
            if n not in used and start <= record["at"] <= position
            and same_question(said, record["text"])
        ), None)
        if voucher is not None and (trust_backend_questions or not _is_question_line(said)):
            used.add(voucher)
            # A line the backend itself spoke. For a booking these include its
            # own consent questions (a silence follow-up, a translated read-back,
            # "would you like to continue with the original?"), and any other
            # backend question has a dedicated handler that answers its yes
            # first. A cancellation is resolved BEFORE those handlers, so there
            # only backend statements ("one moment...") keep the yes bound.
            continue
        return False
    return True


def _is_question_line(text: str) -> bool:
    text = (text or "").strip()
    return "?" in text or bool(_LOOKS_LIKE_QUESTION_RE.search(text))


def booking_consent_context(session: Any) -> bool:
    """The caller's latest turn may be read as an answer to the booking question."""
    question = session.entities.get(OPEN_BOOKING_QUESTION)
    if isinstance(question, dict):
        if question.get("fingerprint") != proposal_fingerprint(get_draft(session)):
            return False  # the question was about a different proposal
        if question.get("ambiguous"):
            return False  # it also asked something else: the yes may answer that
        bound = answers_backend_question(
            session,
            history_len=int(question.get("history_len", 0)),
            question=question.get("text"),
            trusted=list(session.entities.get(BACKEND_PROMPTS) or []),
        )
        if bound is not None:
            return bound
    return not answering_unrelated_question(session)


def is_booking_consent(session: Any, utterance: str | None) -> bool:
    """Plain agreement that answers CARA's question about this booking."""
    return is_affirmative(utterance) and booking_consent_context(session)


def refuses_offer(utterance: str | None) -> bool:
    """The caller turns down what was just offered ("no", "no, don't book that")."""
    text = _normalize(utterance or "")
    if not text:
        return False
    if text in _NEGATIVE:
        return True
    if not _REFUSAL_START_RE.match(text):
        return False
    # "No, Tuesday instead" is a change, handled as a new request.
    return not _BOOKING_FIELD_MODIFICATION.search(text)


def is_negative(utterance: str | None) -> bool:
    """Whether the caller gave a short, unambiguous refusal."""
    return _normalize(utterance or "") in _NEGATIVE


# Anything that could plausibly carry a date, a time, or a scheduling request.
# Deliberately over-inclusive: a false positive costs one model round-trip, a
# false negative would drop a booking on the floor.
_SCHEDULING_HINT = re.compile(
    r"\d|\b(book|booking|schedule|scheduling|appointment|appointments|reschedule|"
    r"cancel|move|change|availability|available|free|slot|opening|time|times|"
    r"today|tomorrow|tonight|morning|afternoon|evening|noon|midday|midnight|"
    r"weekend|next week|this week|am|pm|o'clock|oclock|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|may|june|july|august|september|october|"
    r"november|december|"
    r"massage|facial|treatment|service|session)\b",
    re.IGNORECASE,
)


def mentions_scheduling(utterance: str | None) -> bool:
    """Whether this turn could affect a booking.

    Used to skip the intent-extraction model call on turns that plainly cannot
    change anything — greetings, thanks, "can you hear me?" — which removes a
    full round-trip from the caller's wait on those turns.
    """
    return bool(_SCHEDULING_HINT.search(utterance or ""))


@dataclass
class BookingDraft:
    """The single active booking intent for one call."""

    booking_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    start_iso: str | None = None
    end_iso: str | None = None
    service_description: str | None = None
    caller_name: str | None = None
    caller_email: str | None = None
    # The caller's explicit staff request ("can I book with Sarah"), if any.
    # None means no preference stated; any qualified staff member may be used.
    preferred_staff: str | None = None
    # Authoritative identity resolved from the spa's configured records.
    # Later availability and booking calls use these instead of free-form text.
    service_id: str | None = None
    square_variation_id: str | None = None
    square_variation_version: int | None = None
    provider_id: str | None = None
    duration_minutes: int | None = None
    # Appointment is for this guest when the caller is booking for someone else.
    guest_name: str | None = None
    # Set once the agent has read the full details back to the caller, so a
    # bare "yes" can only confirm details the caller actually heard.
    read_back: bool = False
    # Populated when the intent is persisted. Their presence is what makes a
    # later change a reschedule instead of a new booking.
    appointment_id: str | None = None
    external_booking_id: str | None = None
    cancelled: bool = False
    # Explicit workflow state. Destructive actions must target an appointment
    # identified by the backend rather than reconstructed from transcript text.
    operation_mode: str = "schedule"  # schedule | reschedule | cancel | lookup
    target_appointment_id: str | None = None
    offered_appointment_ids: list[str] = field(default_factory=list)
    # The authoritative provider-confirmed slot for the CURRENT pending
    # request — start time, team member, service variation + version,
    # location, duration. Set ONLY from a real `AvailabilityVerdict.slot`
    # returned by a provider round-trip (Square's SearchAvailability today);
    # never computed or guessed locally. `confirm_booking` pins the final
    # provider recheck to this exact combination so the therapist/service/time
    # the caller already heard read back to them cannot silently change.
    selected_slot: dict[str, Any] | None = None
    # Authoritative alternative slots last offered to the caller, same
    # provenance rule as `selected_slot`. Used only to let the availability
    # guard recognize an alternative the caller might pick next.
    alternative_slots: list[dict[str, Any]] = field(default_factory=list)
    # Last completed provider availability result for this call. Spoken times
    # must come from `slots` here, not from model memory.
    verified_availability: dict[str, Any] | None = None
    # Monotonic generation of the caller's current wish. Bumped whenever a
    # booking-defining field changes so a stale HydroLux slot cannot be
    # confirmed after the caller asked for Swedish Massage.
    draft_revision: int = 0
    selected_slot_revision: int | None = None
    read_back_revision: int | None = None
    verified_fingerprint: str | None = None
    read_back_fingerprint: str | None = None
    confirmation_authorized: bool = False
    provider_verified: bool = False

    @property
    def is_complete(self) -> bool:
        """Enough information to put something on a calendar."""
        return bool(self.start_iso)

    @property
    def is_persisted(self) -> bool:
        return bool(self.appointment_id) and not self.cancelled

    def missing(self) -> list[str]:
        gaps: list[str] = []
        if not self.start_iso:
            gaps.append("date and time")
        return gaps

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BookingDraft":
        if not isinstance(data, dict):
            return cls()
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


DRAFT_ENTITY_KEY = "booking_draft"


def get_draft(session: Any) -> BookingDraft:
    """The call's active draft, created on first use.

    Stored inside `CallSession.entities` so it round-trips through the existing
    Redis JSON without widening the session schema or breaking payloads written
    by an older release.
    """
    draft = BookingDraft.from_dict(session.entities.get(DRAFT_ENTITY_KEY))
    # Recover an intent persisted before this process saw the call (Redis loss,
    # a second worker) from the flat fields the session already carried.
    if draft.appointment_id is None and getattr(session, "appointment_id", None):
        draft.appointment_id = session.appointment_id
        draft.external_booking_id = getattr(session, "external_booking_id", None)
    session.entities[DRAFT_ENTITY_KEY] = draft.to_dict()
    return draft


def save_draft(session: Any, draft: BookingDraft) -> None:
    session.entities[DRAFT_ENTITY_KEY] = draft.to_dict()
    # Mirror onto the flat session fields the dashboard and finalizer read.
    if draft.appointment_id:
        session.appointment_id = draft.appointment_id
        session.external_booking_id = draft.external_booking_id
    if draft.start_iso:
        session.requested_datetime = draft.start_iso
    if draft.service_description:
        session.selected_service = draft.service_description


def intent_key(session: Any, draft: BookingDraft) -> str:
    """Idempotency key for this call's active booking intent.

    Deliberately *not* derived from the date: two different callers may
    legitimately hold the same slot where capacity allows, and the same caller
    may move their own booking several times within one call. Tying the key to
    (call, intent) makes repeated confirmations converge on one row while
    leaving both of those cases expressible.
    """
    return f"{session.call_sid}:{draft.booking_id}"


def stage(session: Any, intent: Any) -> BookingDraft:
    """Fold a newly heard request into the active draft, in place.

    This is the heart of the fix: a caller changing the date mutates the one
    draft rather than opening a second booking, so there is never a Sept 20
    *and* a Sept 22 to reconcile later.
    """
    draft = get_draft(session)
    changed_fields: list[str] = []
    old_service = draft.service_description
    old_start = draft.start_iso
    old_staff = draft.preferred_staff
    old_guest = draft.guest_name

    incoming_operation = getattr(intent, "intent", None)
    if incoming_operation in {"schedule", "reschedule"}:
        if incoming_operation != draft.operation_mode:
            changed_fields.append("operation_mode")
        draft.operation_mode = incoming_operation
        draft.cancelled = False
        if incoming_operation == "schedule":
            # A genuinely new scheduling flow must not inherit a destructive
            # target selected for an earlier cancel/reschedule operation.
            draft.target_appointment_id = None
            draft.offered_appointment_ids = []

    new_start = getattr(intent, "requested_start_iso", None)
    new_end = getattr(intent, "requested_end_iso", None)

    if new_start:
        start_changed = new_start != draft.start_iso
        end_changed = new_end is not None and new_end != draft.end_iso
        if start_changed:
            changed_fields.append("date_time")
        elif end_changed:
            changed_fields.append("duration")
        draft.start_iso = new_start
        if start_changed:
            draft.end_iso = new_end
        elif new_end is not None:
            draft.end_iso = new_end

    elif new_end:
        if new_end != draft.end_iso:
            changed_fields.append("duration")
        draft.end_iso = new_end

    if getattr(intent, "service_description", None):
        if not _same_service_phrase(intent.service_description, draft.service_description):
            changed_fields.append("service")
            draft.square_variation_id = None
            draft.square_variation_version = None
            draft.service_id = None
            draft.duration_minutes = None
            # The end time belonged to the previous service. Keeping it would
            # make "same time, but the 90-minute hot stone instead" inherit
            # the old 60-minute end. Let the new service's duration decide,
            # unless the caller stated an explicit end in this same request.
            if new_end is None:
                draft.end_iso = None
            draft.service_description = intent.service_description
        elif not draft.service_description:
            draft.service_description = intent.service_description

    incoming_name = " ".join((getattr(intent, "caller_name", None) or "").split())
    if incoming_name:
        if incoming_name != (draft.caller_name or ""):
            truth("CALLER_NAME_CAPTURED")
        draft.caller_name = incoming_name
        session.entities.pop("awaiting_caller_name", None)

    if getattr(intent, "caller_email", None):
        draft.caller_email = intent.caller_email

    if getattr(intent, "guest_name", None):
        if intent.guest_name != draft.guest_name:
            changed_fields.append("guest")
        draft.guest_name = intent.guest_name

    incoming_staff = getattr(intent, "preferred_staff", None)

    # Never let a caller-name answer become a therapist preference. The realtime
    # model can occasionally duplicate a person's name into both fields after
    # asking "May I have your full name for the appointment?". Transcript context
    # is authoritative here: if that name was the answer to a caller-name
    # question, it is customer identity only — unless the same utterance also
    # contains an explicit staff request ("My name is Sarah and I'd like to
    # book with Sarah").
    if incoming_staff and _was_answer_to_caller_name_question(session, incoming_staff):
        if not _same_utterance_has_caller_and_staff(session):
            if (
                draft.preferred_staff
                and _normalize(draft.preferred_staff) == _normalize(incoming_staff)
            ):
                draft.preferred_staff = None
                changed_fields.append("staff")
            incoming_staff = None

    if incoming_staff:
        if incoming_staff != draft.preferred_staff:
            changed_fields.append("staff")
        draft.preferred_staff = incoming_staff

    if changed_fields:
        draft.draft_revision = int(draft.draft_revision or 0) + 1
        truth(
            "BOOKING_DRAFT_CHANGED",
            revision=draft.draft_revision,
            changed_fields=",".join(changed_fields),
            old_service=old_service,
            new_service=draft.service_description,
            old_start=old_start,
            new_start=draft.start_iso,
            old_staff=old_staff,
            new_staff=draft.preferred_staff,
            old_guest=old_guest,
            new_guest=draft.guest_name,
        )
        save_draft(session, draft)
        invalidate_booking_proposal(
            session,
            ",".join(changed_fields),
            service_changed="service" in changed_fields,
        )
        draft = get_draft(session)

    save_draft(session, draft)
    return draft


CALLER_NAME_QUESTION = "Can I get your name for the appointment?"

_NAME_LEAD_RE = re.compile(
    r"^(?:my name is|my name's|i am|i'm|this is|it's|it is|the name is|name is)\s+",
    re.IGNORECASE,
)
_NOT_A_CALLER_NAME_RE = re.compile(
    r"\d|\b(?:yes|yeah|yep|yup|no|nope|ok|okay|book|booking|appointment|available|"
    r"tomorrow|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"am|pm|massage|please|sure|correct)\b",
    re.IGNORECASE,
)
_CALLER_NAME_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’.-]*")


def supplied_caller_name(draft: BookingDraft) -> str | None:
    """Name the caller gave for this booking. A phone match is not this."""
    name = " ".join((getattr(draft, "caller_name", None) or "").split())
    return name or None


def caller_name_required_for(draft: BookingDraft) -> bool:
    """New bookings need a name spoken on this call. A move does not."""
    return getattr(draft, "operation_mode", None) != "reschedule"


def recover_caller_name_from_history(session: Any) -> str | None:
    """Keep a name the caller already said, without treating a phone match as one."""
    draft = get_draft(session)
    existing = supplied_caller_name(draft)
    if existing:
        return existing
    history = list(getattr(session, "history", None) or [])
    for idx, turn in enumerate(history):
        if not isinstance(turn, dict) or turn.get("role") != "user":
            continue
        content = str(turn.get("content") or "")
        lead = bool(_NAME_LEAD_RE.match(content.strip()))
        followed_question = False
        if not lead:
            for previous in range(idx - 1, -1, -1):
                prior = history[previous]
                if not isinstance(prior, dict):
                    continue
                if prior.get("role") == "assistant":
                    followed_question = bool(
                        _CALLER_NAME_QUESTION.search(str(prior.get("content") or ""))
                    )
                    break
                if prior.get("role") == "user":
                    break
        if not lead and not followed_question:
            continue
        name = spoken_name_from_utterance(content)
        if not name:
            continue
        draft.caller_name = name
        save_draft(session, draft)
        session.entities.pop("awaiting_caller_name", None)
        truth("CALLER_NAME_CAPTURED")
        return name
    return None


def spoken_name_from_utterance(utterance: str | None) -> str | None:
    """A short name answer. Times, yes/no, and booking words are not names."""
    text = " ".join((utterance or "").split())
    text = _NAME_LEAD_RE.sub("", text).strip(" .,!?:;")
    if not text or _NOT_A_CALLER_NAME_RE.search(text):
        return None
    words = text.split()
    if not 1 <= len(words) <= 4:
        return None
    if not all(_CALLER_NAME_WORD_RE.fullmatch(word) for word in words):
        return None
    return text


def capture_caller_name_answer(session: Any, utterance: str | None) -> str | None:
    """Store a name only after this call asked for one and the caller answered."""
    if not session.entities.get("awaiting_caller_name"):
        return None
    draft = get_draft(session)
    if supplied_caller_name(draft):
        session.entities.pop("awaiting_caller_name", None)
        return None
    name = spoken_name_from_utterance(utterance)
    if not name:
        return None
    draft.caller_name = name
    save_draft(session, draft)
    session.entities.pop("awaiting_caller_name", None)
    truth("CALLER_NAME_CAPTURED")
    return name


def mark_read_back(session: Any, question: str | None = None) -> BookingDraft:
    """Record that the agent has just recited the full details to the caller.

    ``question`` is the line that asked for the go-ahead; a later yes binds to
    it (see `booking_consent_context`).
    """
    open_question = session.entities.get(OPEN_BOOKING_QUESTION)
    draft_now = get_draft(session)
    if question is not None or not (
        isinstance(open_question, dict)
        and draft_now.selected_slot
        and open_question.get("fingerprint") == proposal_fingerprint(draft_now)
    ):
        note_booking_question(session, question)
    draft = get_draft(session)
    if caller_name_required_for(draft) and not supplied_caller_name(draft):
        truth("CALLER_NAME_REQUIRED")
        return draft
    if (
        draft.is_complete
        and draft.selected_slot
        and draft.provider_verified
        and draft.selected_slot_revision == draft.draft_revision
        and draft.verified_fingerprint
    ):
        fingerprint = proposal_fingerprint(draft)
        if fingerprint == draft.verified_fingerprint:
            draft.read_back = True
            draft.read_back_revision = draft.draft_revision
            draft.read_back_fingerprint = fingerprint
            save_draft(session, draft)
            truth(
                "BOOKING_READBACK_ARMED",
                revision=draft.draft_revision,
                fingerprint=fingerprint,
            )
    return draft


def start_new_intent(session: Any) -> BookingDraft:
    """Begin a genuinely separate second appointment.

    The only way to get a second appointment out of one call, and it has to be
    asked for explicitly — everything else edits the intent already in flight.
    """
    draft = BookingDraft()
    save_draft(session, draft)
    session.entities[DRAFT_ENTITY_KEY] = draft.to_dict()
    # The flat pointers describe the previous intent; clear them so the new
    # intent starts unpersisted.
    session.appointment_id = None
    session.external_booking_id = None
    session.confirmed_datetime = None
    # These compatibility fields mirror the *active* draft.  Leaving them
    # populated makes a separate second appointment appear to inherit the
    # service and time from the appointment that just completed, even though
    # the new BookingDraft itself is empty.
    session.requested_datetime = None
    session.selected_service = None
    session.selected_duration = None
    session.entities.pop("active_appointment_id", None)
    session.entities.pop("caller_confirmed_revision", None)
    session.entities.pop("caller_confirmed_fingerprint", None)
    session.entities.pop("card_policy_explained", None)
    session.entities.pop("card_email_collecting", None)
    session.entities.pop("card_email_confirmed", None)
    session.entities.pop("card_link_consent_pending_revision", None)
    session.entities.pop("card_link_consent_authorized_revision", None)
    # Consultation and caller-grounding state belongs to one booking intent.
    # Clear it here, at the shared state transition, so every path that starts
    # another appointment (including automatic post-booking routing) gets the
    # same clean slate.  The persisted appointment itself remains untouched in
    # the database; only pointers and transient state for the active draft are
    # reset.
    for key in (
        "consultation_state",
        "consultation_states",
        "consultation_completed_kinds",
        "consultation_addon_names",
        "consultation_addon_pending",
        "consultation_booking_request",
        "consultation_question_turns",
        "consultation_answer_attempts",
        "consultation_permission_turns",
        "consultation_permission_attempts",
        "consultation_duration_offer_turns",
        "consultation_duration_attempts",
        "consultation_recommendation_pending",
        "consultation_menu_choice_pending",
        "caller_grounded_requested_time",
        "caller_grounded_service_durations",
        "caller_grounded_exact_times",
        "requested_availability_window",
        "smart_enhancement",
        "enhancement_base_time_accepted",
        "accepted_booking_offer",
        "spoken_booking_offer",
        "spoken_booking_choices",
    ):
        session.entities.pop(key, None)
    # Use an explicit tombstone rather than deleting this key.  The realtime
    # date resolver may still have the prior appointment's words in transcript
    # history; absence would let it fall back to that old turn and resurrect
    # the previous date for the new appointment.
    session.entities["caller_grounded_requested_date"] = {
        "date": None,
        "source": "cleared",
    }
    return draft


# --------------------------------------------------------------------------- #
# After a booking: change the existing appointment, or add another one?
# --------------------------------------------------------------------------- #
# Session entity set while CARA is waiting for the caller to answer the
# "change it or add another?" question.
BOOKING_CHANGE_CLARIFICATION = "booking_change_clarification"
# Session entity set once the caller has chosen to change the existing
# appointment, so follow-up adjustments ("how about 4 then?") are not asked
# about again while that change is still being arranged.
BOOKING_CHANGE_IN_PROGRESS = "booking_change_in_progress"

_ADDITIONAL_REQUEST_RE = re.compile(
    r"\b(?:another|second|additional|one more|also|as well|in addition|separate|"
    r"extra|a new one|both|add(?:ing)?\s+(?:a|an|another|one)|book\s+(?:a|an|another)|"
    r"for my (?:wife|husband|partner|friend|mom|mum|mother|dad|father|daughter|son|"
    r"sister|brother|boyfriend|girlfriend|colleague)|for (?:him|her|them))\b",
    re.IGNORECASE,
)
# Unmistakable "change what I have" wording.
_MODIFY_REQUEST_RE = re.compile(
    r"\b(?:instead|change|changing|move|moving|reschedule|switch|swap|push|"
    r"bump|make (?:it|that)|i meant|i mean|meant to say|existing|"
    r"not (?:on |at )?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"today|tomorrow|\d)|can'?t make|cannot make|won'?t make)\b",
    re.IGNORECASE,
)
# Words that often, but not always, mean a change: "anything later?" can
# just as well be shopping for a second appointment.
_MAYBE_MODIFY_RE = re.compile(
    r"\b(?:rather|earlier|later|different (?:time|day|date))\b",
    re.IGNORECASE,
)
# Answers to the "change it, add another, or keep it as is?" question.
# "Keep" is NOT a request for anything new: it preserves the booking as it is.
_ANSWER_KEEP_RE = re.compile(
    r"\b(?:keep(?:ing)?\s+(?:it|that|this|everything|what i (?:have|had|booked)|my (?:\w+\s+)?(?:appointment|booking|time|slot|reservation|visit|original)|"
    r"the (?:original|same|appointment|booking|time|slot|reservation|visit|first|one i (?:have|had|booked))|"
    r"(?:it|that|this) (?:as|the way|like)\b)(?!\s+in\s+mind)(?!\s+posted)|"
    r"(?:just|please|no,?|ok(?:ay)?,?|i'?ll|i will|i'?d (?:rather|like to)|let'?s|we'?ll)\s+keep(?:\s+it)?\s*[.!]?$|^\s*keep\s*[.!]?$|"
    r"never ?mind|nevermind|leave (?:it|that|everything|(?:my|the|our) (?:\w+\s+)?(?:appointment|booking|reservation|visit|time|slot)s?(?= alone| as | be\b| how| the way))|"
    r"forget (?:it|that|about it)|"
    r"d[eé]jalo (?:as[ií]|como est[aá])|no (?:lo )?cambies|olv[ií]dalo|"
    r"as (?:it|that) is|the way (?:it|that) is|as is|stays? the same|the same as before|"
    r"no changes?|don'?t change|do not change|don'?t bother|the original)\b",
    re.IGNORECASE,
)
_ANSWER_ADDITIONAL_RE = re.compile(
    r"\b(?:add|adding|another|separate|both|additional|second|also|extra|"
    r"a new one|new (?:appointment|booking)|book (?:a|an|another) (?:new|second|separate|additional|extra)?)\b",
    re.IGNORECASE,
)
_ANSWER_MODIFY_RE = re.compile(
    r"\b(?:change|update|modify|replace|move|reschedule|switch|instead|"
    r"first one|the first)\b",
    re.IGNORECASE,
)
_NEGATED_CHANGE_RE = re.compile(
    r"\b(?:don'?t|do not|no|not|without|never) (?:change|changing|move|moving|update|switch)\b",
    re.IGNORECASE,
)


def _caller_text(utterance: str | None) -> str:
    return (utterance or "").replace("’", "'")


def classify_post_booking_request(utterance: str | None) -> str:
    """After a booking exists: does the caller want to change it or add one?

    Returns "modify", "maybe_modify", "additional", or "ambiguous". Only the
    caller's own words count. Clear change wording is "modify"; wording like
    "later" or "rather" is only "maybe_modify"; both kinds of cue, or none, is
    "ambiguous" and CARA must ask before anything changes. "Keep it" /
    "never mind" is recognised separately by `is_keep_request`.
    """
    text = _caller_text(utterance)
    additional = bool(_ADDITIONAL_REQUEST_RE.search(text))
    if _MODIFY_REQUEST_RE.search(text):
        return "ambiguous" if additional else "modify"
    if _MAYBE_MODIFY_RE.search(text):
        return "ambiguous" if additional else "maybe_modify"
    return "additional" if additional else "ambiguous"


def classify_change_question_answer(utterance: str | None) -> str:
    """The caller's answer to "change it, add another, or keep it as is?".

    "keep"       - "keep it", "never mind", "leave it alone", "no, keep the
                   original": preserve the booking, create and change nothing.
    "additional" - "add another", "a new one", "keep it and add one".
    "modify"     - "change it", "update it", "the first one".
    "ambiguous"  - anything else, or conflicting cues; ask again.
    """
    text = _caller_text(utterance)
    additional = bool(_ANSWER_ADDITIONAL_RE.search(text))
    # "Don't change it" is a keep, not a change request.
    modify = bool(_ANSWER_MODIFY_RE.search(_NEGATED_CHANGE_RE.sub(" ", text)))
    keep = bool(_ANSWER_KEEP_RE.search(text)) or bool(_NEGATED_CHANGE_RE.search(text))
    if additional and not modify:
        return "additional"  # "keep it and add one" asks for an additional booking
    if modify and not additional and not keep:
        return "modify"
    if keep and not additional and not modify:
        return "keep"
    return "ambiguous"


def is_keep_request(utterance: str | None) -> bool:
    """The caller wants the existing booking left exactly as it is."""
    return classify_change_question_answer(utterance) == "keep"


def _booked_when(booked_iso: str | None, tz: Any) -> str:
    parsed = _parse_provider_iso(booked_iso)
    if parsed is None:
        return ""
    local = parsed.astimezone(tz) if parsed.tzinfo else parsed
    return f" on {local.strftime('%A')} at {spoken_slot_time(booked_iso, tz)}"


def _booked_service(service: str | None) -> str:
    return re.sub(r"\s*\(\d+\s*min\)", "", (service or "appointment")).strip() or "appointment"


def booking_change_question(
    service: str | None, booked_iso: str | None, tz: Any, *, attempt: int = 1
) -> str:
    """The one question that settles change / add / keep, naming the BOOKED visit."""
    service = _booked_service(service)
    when = _booked_when(booked_iso, tz)
    if attempt > 1:
        return (
            "I want to be sure I don't change the wrong thing. For your "
            f"{service}{when}, you can say 'change it', 'add a new one', or "
            "'keep it as is'."
        )
    return (
        f"Just to make sure I get this right: would you like to change your {service}"
        f"{when}, book an additional appointment, or keep everything as it is?"
    )


def kept_booking_speech(service: str | None, booked_iso: str | None, tz: Any) -> str:
    """Confirmation that nothing was changed or added."""
    return (
        f"No problem. Your {_booked_service(service)}{_booked_when(booked_iso, tz)} "
        "stays exactly as it is, and nothing new was booked. Is there anything else "
        "I can help you with?"
    )


def additional_booking_notice(service: str | None, booked_iso: str | None, tz: Any) -> str:
    """Said before an additional appointment can be confirmed."""
    return (
        "Just to be clear, this would be an additional appointment. Your "
        f"{_booked_service(service)}{_booked_when(booked_iso, tz)} stays booked as well."
    )


def proposal_fingerprint(draft: BookingDraft) -> str:
    """Stable hash of the CURRENT proposal the provider would create."""
    slot = draft.selected_slot or {}
    payload = {
        "service": (draft.service_description or "").strip().casefold(),
        "visit_segments": slot.get("visit_segments") or [],
        "variation_id": str(slot.get("service_variation_id") or ""),
        "variation_version": str(slot.get("service_variation_version") or ""),
        "start": str(slot.get("start") or draft.start_iso or ""),
        "duration": str(slot.get("duration_minutes") or ""),
        "location_id": str(slot.get("location_id") or ""),
        "team_member_id": str(slot.get("team_member_id") or ""),
        "guest": (draft.guest_name or "").strip().casefold(),
        "staff": (draft.preferred_staff or "").strip().casefold(),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def bind_verified_slot(session: Any, slot: dict[str, Any] | None) -> BookingDraft:
    """Record a provider-confirmed slot against the CURRENT draft revision."""
    draft = get_draft(session)
    draft.selected_slot = dict(slot) if slot else None
    draft.provider_verified = bool(slot)
    if slot:
        draft.selected_slot_revision = draft.draft_revision
        draft.verified_fingerprint = proposal_fingerprint(draft)
        truth(
            "AVAILABILITY_RESULT",
            available=True,
            draft_revision=draft.draft_revision,
            fingerprint=draft.verified_fingerprint,
            variation_id=slot.get("service_variation_id"),
            service=draft.service_description,
        )
        truth(
            "SERVICE_VERIFIED",
            service=draft.service_description,
            variation_id=slot.get("service_variation_id"),
            revision=draft.draft_revision,
        )
    else:
        draft.selected_slot_revision = None
        draft.verified_fingerprint = None
    draft.read_back = False
    draft.read_back_revision = None
    draft.read_back_fingerprint = None
    draft.confirmation_authorized = False
    session.entities.pop("caller_confirmed_revision", None)
    session.entities.pop("caller_confirmed_fingerprint", None)
    session.entities.pop("card_policy_explained", None)
    session.entities.pop("card_link_consent_pending_revision", None)
    session.entities.pop("card_link_consent_authorized_revision", None)
    save_draft(session, draft)
    return draft


def _availability_invalidation_reason(reason: str) -> str:
    text = (reason or "").lower()
    if "service" in text:
        return "service_changed"
    if "staff" in text:
        return "staff_changed"
    if "location" in text:
        return "location_changed"
    if "duration" in text:
        return "duration_changed"
    if "date" in text or "time" in text:
        return "date_changed"
    return reason or "context_changed"


def invalidate_booking_proposal(session: Any, reason: str, *, service_changed: bool = False) -> BookingDraft:
    """Drop every provider-backed proposal tied to a previous wish."""
    draft = get_draft(session)
    had_verified = bool(draft.verified_availability or draft.alternative_slots or draft.selected_slot)
    truth(
        "BOOKING_PROPOSAL_INVALIDATED",
        reason=reason,
        old_revision=draft.draft_revision,
        service=draft.service_description,
    )
    if had_verified:
        truth(
            "AVAILABILITY_STATE_INVALIDATED",
            call_sid=getattr(session, "call_sid", None),
            reason=_availability_invalidation_reason(reason),
        )
    session.entities.pop("accepted_booking_offer", None)
    session.entities.pop("spoken_booking_offer", None)
    session.entities.pop("spoken_booking_choices", None)
    draft.selected_slot = None
    draft.alternative_slots = []
    draft.verified_availability = None
    draft.read_back = False
    draft.provider_verified = False
    draft.selected_slot_revision = None
    draft.read_back_revision = None
    draft.verified_fingerprint = None
    draft.read_back_fingerprint = None
    draft.confirmation_authorized = False
    session.entities.pop("caller_confirmed_revision", None)
    session.entities.pop("caller_confirmed_fingerprint", None)
    if service_changed and hasattr(session, "selected_duration"):
        session.selected_duration = None
    status = getattr(session, "booking_status", None)
    if status not in {"booked", "rescheduled"}:
        session.booking_status = "collecting_details"
    save_draft(session, draft)
    return draft


def record_pure_confirmation(session: Any, utterance: str | None = None) -> bool:
    """One-shot authorization to call create_booking for the current revision."""
    draft = get_draft(session)
    if utterance is not None and not is_affirmative(utterance):
        truth("CONFIRMATION_REJECTED", reason="modification_present", utterance=utterance)
        return False
    if utterance is not None and not booking_consent_context(session):
        truth("CONFIRMATION_REJECTED", reason="answer_to_unrelated_question", utterance=utterance)
        return False
    if caller_name_required_for(draft) and not supplied_caller_name(draft):
        truth("CALLER_NAME_REQUIRED")
        truth("CONFIRMATION_REJECTED", reason="caller_name_missing")
        return False
    if not draft.read_back or not draft.provider_verified or not draft.selected_slot:
        truth("CONFIRMATION_REJECTED", reason="readback_or_slot_missing")
        return False
    fingerprint = proposal_fingerprint(draft)
    if (
        draft.draft_revision != draft.selected_slot_revision
        or draft.draft_revision != draft.read_back_revision
        or fingerprint != draft.verified_fingerprint
        or fingerprint != draft.read_back_fingerprint
    ):
        truth(
            "CONFIRMATION_REJECTED",
            reason="stale_revision",
            draft_revision=draft.draft_revision,
            selected_slot_revision=draft.selected_slot_revision,
            read_back_revision=draft.read_back_revision,
        )
        return False
    draft.confirmation_authorized = True
    session.entities["caller_confirmed_revision"] = draft.draft_revision
    session.entities["caller_confirmed_fingerprint"] = fingerprint
    save_draft(session, draft)
    truth("CONFIRMATION_ACCEPTED", revision=draft.draft_revision, fingerprint=fingerprint)
    return True


def arm_verified_proposal(session: Any) -> BookingDraft:
    """Mark the current provider-verified slot as read back and caller-confirmed.

    Used by tests and by the voice path after an explicit pure "yes".
    """
    mark_read_back(session)
    # Explicit authorization by the calling code (tests, or a caller yes that
    # was already checked): not re-derived from conversation position.
    record_pure_confirmation(session)
    return get_draft(session)


def confirmation_block_reason(session: Any) -> str | None:
    """Hard stop before create_booking. None means the write is allowed."""
    draft = get_draft(session)
    status = getattr(session, "booking_status", None)
    if draft.is_persisted and status in {"booked", "rescheduled"}:
        return None
    if caller_name_required_for(draft) and not supplied_caller_name(draft):
        truth("CALLER_NAME_REQUIRED")
        return "caller_name_missing"
    if (
        caller_name_required_for(draft)
        and session.entities.get("card_on_file_required")
        and not session.entities.get("card_policy_explained")
    ):
        truth("CARD_POLICY_REQUIRED", call_sid=getattr(session, "call_sid", None))
        return "card_policy_not_explained"
    if (
        caller_name_required_for(draft)
        and session.entities.get("card_on_file_required")
        and session.entities.get("card_link_consent_authorized_revision")
        != draft.draft_revision
    ):
        truth("CARD_LINK_CONSENT_REQUIRED", call_sid=getattr(session, "call_sid", None))
        return "card_link_consent_missing"
    if session.entities.get("card_on_file_required") and session.entities.get("card_link_delivery") == "email":
        from app.services.card_email_conversation import confirmed_email
        if not confirmed_email(session):
            return "card_link_consent_missing"
    if status != "awaiting_confirmation":
        return "not_awaiting_confirmation"
    if not draft.start_iso or not draft.service_description:
        return "draft_incomplete"
    if not draft.selected_slot or not draft.provider_verified:
        return "selected_slot_missing"
    if draft.selected_slot_revision != draft.draft_revision:
        return "stale_revision"
    fingerprint = proposal_fingerprint(draft)
    if draft.verified_fingerprint != fingerprint:
        return "stale_verified_fingerprint"
    if not draft.read_back or draft.read_back_revision != draft.draft_revision:
        return "readback_missing"
    if draft.read_back_fingerprint != fingerprint:
        return "stale_readback_fingerprint"
    authorized_rev = session.entities.get("caller_confirmed_revision")
    authorized_fp = session.entities.get("caller_confirmed_fingerprint")
    if (
        not draft.confirmation_authorized
        or authorized_rev != draft.draft_revision
        or authorized_fp != fingerprint
    ):
        return "confirmation_not_authorized"
    return None


def consume_confirmation_authorization(session: Any) -> None:
    """Confirmation is one-shot: a later yes cannot reuse this grant."""
    draft = get_draft(session)
    draft.confirmation_authorized = False
    session.entities.pop("caller_confirmed_revision", None)
    session.entities.pop("caller_confirmed_fingerprint", None)
    save_draft(session, draft)


# --------------------------------------------------------------------------- #
# Hard availability-claim guard
#
# The booking-confirmation guard elsewhere in this codebase (telephony.py's
# `_authoritative_reply`, looks_like_unverified_booking_success) stops
# the agent from ever claiming a booking is confirmed unless a persisted
# appointment backs it. This is the availability-side counterpart: it stops
# the agent from claiming a SPECIFIC time is open/available/free/offered
# unless that exact time is something a real provider round-trip actually
# returned for this call. Business hours are not availability, and being
# inside opening hours never implies a therapist is free.
#
# The authoritative source is `BookingDraft.selected_slot` /
# `alternative_slots`, which are populated exclusively from
# `AvailabilityVerdict.slot` — itself populated exclusively by a real
# provider search (see `SquareAdapter.check_availability`). Nothing here
# computes or guesses a time; it only decides whether the agent's own words
# are backed by that state.
# --------------------------------------------------------------------------- #

_CLOCK_TIME_PATTERN = re.compile(
    r"\b\d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?)\b|\b([01]?\d):([0-5]\d)\b",
    re.IGNORECASE,
)

# Phrasing that asserts a specific time is bookable, as opposed to merely
# repeating back what the caller asked for ("you said 2pm") or asking a
# question ("does 2pm work for you?" is handled by the confirmation-request
# regex elsewhere — this list is deliberately about ASSERTING availability).
_AVAILABILITY_CLAIM_MARKERS = re.compile(
    r"\b(is available|are available|is open|we have|i have|got an opening|"
    r"have an opening|open at|free at|works for|next opening|next available|"
    r"that time is free|welcome to come in at|can (?:do|fit you in)|"
    r"i can (?:offer|do)|that works)\b",
    re.IGNORECASE,
)


def _time_signatures(local_dt: datetime) -> set[str]:
    """A handful of plausible spoken/written renderings of one local time,
    so the guard can recognize this time inside free-form agent text without
    needing the agent to use one exact format."""
    h12 = local_dt.strftime("%I:%M%p").lstrip("0").lower()  # "2:00pm"
    no_minutes = h12.replace(":00", "")  # "2pm" (only when :00)
    return {
        h12,
        no_minutes,
        h12[:-2] + " " + h12[-2:],  # "2:00 pm"
        no_minutes[:-2] + " " + no_minutes[-2:],  # "2 pm"
        local_dt.strftime("%H:%M"),  # "14:00"
    }


def _parse_provider_iso(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def authorized_time_signatures(session: Any, tz: Any) -> set[str]:
    """Every time the agent may honestly claim is available on this call.

    Built entirely from provider-confirmed state (`selected_slot`,
    `alternative_slots`) plus the caller's own stated request and any
    already-persisted appointment time — never from anything the model
    computed itself.
    """
    draft = get_draft(session)
    signatures: set[str] = set()

    def _add(iso_value: str | None) -> None:
        parsed = _parse_provider_iso(iso_value)
        if parsed is None:
            return
        local = parsed.astimezone(tz) if parsed.tzinfo else parsed
        signatures.update(_time_signatures(local))

    if draft.selected_slot:
        _add(draft.selected_slot.get("start"))
    for slot in draft.alternative_slots or []:
        _add(slot.get("start"))
    verified = draft.verified_availability or {}
    for slot in verified.get("slots") or []:
        if isinstance(slot, dict):
            _add(slot.get("start"))
    _add(getattr(session, "confirmed_datetime", None))
    return signatures


def spoken_slot_time(iso_value: str | None, tz: Any) -> str | None:
    parsed = _parse_provider_iso(iso_value)
    if parsed is None:
        return None
    local = parsed.astimezone(tz) if parsed.tzinfo else parsed
    label = local.strftime("%I:%M %p").lstrip("0")
    if label.endswith(":00 AM") or label.endswith(":00 PM"):
        label = label.replace(":00 ", " ")
    return label


def _join_spoken_times(labels: list[str]) -> str:
    if not labels:
        return ""
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def verified_slot_labels(session: Any, tz: Any, *, limit: int = 3) -> list[str]:
    """Spoken clock times from the last completed provider result only."""
    draft = get_draft(session)
    slots: list[dict[str, Any]] = []
    verified = draft.verified_availability or {}
    for slot in verified.get("slots") or []:
        if isinstance(slot, dict):
            slots.append(slot)
    if not slots:
        slots = [slot for slot in (draft.alternative_slots or []) if isinstance(slot, dict)]
        if draft.selected_slot:
            slots = [draft.selected_slot, *slots]
    labels: list[str] = []
    for slot in slots:
        label = spoken_slot_time(slot.get("start"), tz)
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def grounded_availability_speech(session: Any, tz: Any) -> str | None:
    labels = verified_slot_labels(session, tz)
    if not labels:
        return None
    return f"I have {_join_spoken_times(labels)}."


CARD_LINK_PERMISSION_QUESTION = "Are you okay with me sending you a secure link for this?"


CARD_ON_FILE_POLICY = (
    "To reserve your appointment, we’ll just need to place a card on file. "
    "This secure card collection step does not charge your card. "
    "Any cancellation or deposit terms depend on this business's configured policy. "
    "If you need to add a card, I can text you a secure link after reserving the appointment. "
    "You can complete it after this call; please don't read card details aloud. "
    + CARD_LINK_PERMISSION_QUESTION
)
CARD_ON_FILE_HESITANT = (
    "The card is kept securely on file. Any applicable fees or deposits depend on "
    "this business's configured policy; I can check those terms for you."
)


def _same_service_phrase(left: str | None, right: str | None) -> bool:
    """Case, spacing, and punctuation do not make a service a different request."""
    def key(value: str | None) -> str:
        return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())
    left_key = key(left)
    right_key = key(right)
    return bool(left_key) and left_key == right_key


_CARD_HESITANT_RE = re.compile(
    r"\b(?:do i have to|why do you need|why is that|not sure|rather not|"
    r"don'?t want|do not want|without a card|is that necessary|"
    r"will you charge|are you going to charge|i(?:'m| am) not comfortable)\b",
    re.IGNORECASE,
)


def looks_card_hesitant(utterance: str | None) -> bool:
    """Uncertainty about the card. A plain yes or no is not this."""
    return bool(_CARD_HESITANT_RE.search(utterance or ""))


def booking_card_speech(card_status: str | None, card_sms: str | None) -> str:
    """Post-booking sentence for the SMS that actually happened.

    The card-on-file policy is spoken before confirmation. This line only
    reports whether the secure text was sent.
    """
    status = (card_status or "").strip().lower()
    sms = (card_sms or "").strip().lower()
    if status == "pending_card" and sms == "email_accepted":
        return (
            " Your secure card-link email has been accepted for sending. Please check your inbox and spam folder."
            " You can complete it after this call. Your reservation is pending your card on file."
        )
    if status == "pending_card" and sms.startswith("email_"):
        return " I couldn't verify that the secure card-link email was sent. Your reservation remains in place; please contact the spa for help adding your card."
    if status == "pending_card" and sms == "sent":
        return (
            " I've also sent you a secure text link to add your card on file. "
            "You can complete it after this call. Your reservation is pending your card on file."
        )
    if status == "pending_card" and sms == "failed":
        return " I wasn't able to send the secure card link just now."
    return ""


def backend_spoke_since_caller(session: Any, text: str | None) -> bool:
    """``text`` is a line the backend itself spoke since the caller last spoke."""
    history = list(getattr(session, "history", None) or [])
    since_caller = max((i + 1 for i, item in enumerate(history) if item.get("role") == "user"), default=0)
    return bool(text) and any(
        record["at"] >= since_caller and same_question(text, record["text"])
        for record in _prompt_records(session.entities.get(BACKEND_PROMPTS))
    )


def canonical_read_back(session: Any, tz: Any) -> str | None:
    """The backend's own single-question read-back of the current proposal.

    Only a question the backend itself authored can become the booking
    question a yes answers: model wording may carry a second question in any
    form ("...or just text you our address?", "let me know if...").
    """
    draft = get_draft(session)
    if not (draft.selected_slot and draft.provider_verified):
        return None
    label = spoken_slot_time(draft.selected_slot.get("start"), tz)
    if not label:
        return None
    service = (draft.service_description or "your appointment").strip()
    if getattr(draft, "operation_mode", None) == "reschedule":
        return (f"{label} is available for {service}. Nothing has been changed yet. "
                "Would you like me to move your appointment to that time?")
    return f"{label} is available for {service}. Nothing is booked yet. Would you like me to book that?"


def authoritative_availability_speech(session: Any, tz: Any) -> str | None:
    """Caller-facing sentence for a completed Square check.

    An exact verified slot is spoken by itself. Otherwise the sentence uses
    only the provider alternatives stored on the draft.
    """
    draft = get_draft(session)
    service = (draft.service_description or "that service").strip()
    if draft.selected_slot and draft.provider_verified:
        label = spoken_slot_time(draft.selected_slot.get("start"), tz)
        if label:
            return (
                f"{label} is available for {service}. "
                "Nothing is booked yet. Would you like me to book that?"
            )
    alternatives = [
        spoken_slot_time(slot.get("start"), tz)
        for slot in (draft.alternative_slots or [])
        if isinstance(slot, dict) and spoken_slot_time(slot.get("start"), tz)
    ]
    requested = spoken_slot_time(draft.start_iso, tz) or "That time"
    if alternatives:
        return (
            f"{requested} isn't available, but I have {_join_spoken_times(alternatives[:3])}."
        )
    if draft.start_iso and not draft.selected_slot:
        return (
            f"I don't see an opening at {requested}. "
            "Would you like to try another day?"
        )
    return None


def remember_verified_availability(
    session: Any,
    slots: list[dict[str, Any]],
    *,
    service: str | None,
    staff: str | None,
    location_id: str | None,
    duration_minutes: int | None,
    date_iso: str | None,
    source: str,
) -> BookingDraft:
    """Replace the call's verified openings with this completed provider result."""
    draft = get_draft(session)
    cleaned = [slot for slot in slots if isinstance(slot, dict) and slot.get("start")]
    previous = draft.verified_availability or {}
    staff_label = staff or "ANY"
    changed = bool(previous) and (
        (previous.get("service") or None) != (service or None)
        or (previous.get("date") or None) != (date_iso or None)
        or (previous.get("staff") or "ANY") != staff_label
        or (previous.get("location_id") or None) != (location_id or previous.get("location_id"))
    )
    if changed:
        reason = "service_changed"
        if (previous.get("date") or None) != (date_iso or None):
            reason = "date_changed"
        elif (previous.get("staff") or "ANY") != staff_label:
            reason = "staff_changed"
        elif (previous.get("location_id") or None) != (location_id or None) and location_id:
            reason = "location_changed"
        truth(
            "AVAILABILITY_STATE_INVALIDATED",
            call_sid=getattr(session, "call_sid", None),
            reason=reason,
        )
    variation = next((slot.get("service_variation_id") for slot in cleaned if slot.get("service_variation_id")), None)
    resolved_location = location_id or next(
        (slot.get("location_id") for slot in cleaned if slot.get("location_id")),
        None,
    )
    draft.verified_availability = {
        "service_variation_id": variation,
        "service": service,
        "date": date_iso,
        "location_id": resolved_location,
        "staff": staff_label,
        "duration_minutes": duration_minutes,
        "slots": cleaned,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
    }
    draft.alternative_slots = cleaned
    save_draft(session, draft)
    return draft


def contains_unauthorized_availability_claim(reply: str, session: Any, tz: Any) -> bool:
    """True when `reply` asserts a specific time is available/open/free and
    that time is not backed by any provider-confirmed state on this call.

    Deliberately conservative in the other direction too: with no claim-marker
    or no clock-time-like substring at all, this returns False rather than
    guessing — the same posture as the existing booking-confirmation guard,
    which only acts on its own narrow phrase list rather than every mention of
    money, dates, or scheduling words.
    """
    # A negative statement about the requested slot is not an opening claim.
    # Strip only that clause; a later positive claim still needs verification.
    reply = re.sub(
        r"(?:" + _CLOCK_TIME_PATTERN.pattern + r")\s+(?:isn't|isn’t|is not|wasn't|was not)\s+(?:available|open|free)",
        "", reply or "", flags=re.IGNORECASE,
    )
    if not reply or not _AVAILABILITY_CLAIM_MARKERS.search(reply):
        return False
    if not _CLOCK_TIME_PATTERN.search(reply):
        return False

    authorized = authorized_time_signatures(session, tz)
    if not authorized:
        # A claim naming a specific time with nothing provider-confirmed yet
        # on this call is unconditionally unauthorized: there is nothing it
        # could legitimately be restating.
        return True

    text = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", reply.lower())
    return not any(signature in text for signature in authorized)


_POST_MASSAGE_FACIAL_AVAILABILITY_CLAIM = re.compile(
    r"(?:"
    r"\b(?:our\s+)?esthetician\b.{0,70}\b(?:available|open|free)\b.{0,80}"
    r"\b(?:after|following)\b.{0,40}\bmassage\b|"
    r"\b(?:facial|esthetician)\b.{0,70}\b(?:available|open|free|room)\b.{0,80}"
    r"\b(?:after|following)\b.{0,40}\bmassage\b|"
    r"\b(?:add|fit\s+in)\b.{0,45}\bfacial\b.{0,55}"
    r"\b(?:right\s+)?after\b.{0,35}\bmassage\b|"
    r"\boption\s+is\s+open\b.{0,80}\bfacial\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def contains_unverified_post_massage_facial_offer(reply: str, session: Any) -> bool:
    """Block a sequential facial availability claim without an atomic visit.

    A separately available facial does not prove that it starts when the
    massage ends or that the required esthetician/resource remains available.
    Only the provider-returned ``visit_segments`` structure can authorize this
    speech.
    """
    if not reply or not _POST_MASSAGE_FACIAL_AVAILABILITY_CLAIM.search(reply):
        return False
    if re.search(
        r"\b(?:check|see|find\s+out)\b.{0,35}\b(?:whether|if)\b",
        reply,
        re.IGNORECASE,
    ):
        # Asking permission to perform the provider lookup is not an
        # availability claim.
        return False

    draft = get_draft(session)
    if not draft.provider_verified or not draft.selected_slot:
        return True
    segments = draft.selected_slot.get("visit_segments") or []
    if len(segments) < 2:
        return True

    spoken = " ".join(re.findall(r"[a-z0-9]+", reply.casefold()))

    def service_key(value: object) -> str:
        label = re.sub(
            r"\b(?:\d+|thirty|sixty|ninety)\s*(?:-|\s)*(?:minute|minutes|min|mins)\b",
            " ",
            str(value or "").casefold(),
        )
        return " ".join(re.findall(r"[a-z0-9]+", label))

    for first, second in zip(segments, segments[1:]):
        first_name = str(first.get("service_name") or "").casefold()
        second_name = str(second.get("service_name") or "").casefold()
        if "massage" not in first_name or "facial" not in second_name:
            continue
        first_end = _parse_provider_iso(first.get("end"))
        second_start = _parse_provider_iso(second.get("start"))
        if (
            first_end is not None
            and second_start is not None
            and second_start >= first_end
            and second.get("team_member_id")
            and service_key(second.get("service_name")) in spoken
        ):
            return False
    return True


# Success-only booking claims. Do NOT match bare "booked"/"confirmed"/"scheduled":
# after a calendar conflict the receptionist must be able to say "that time is
# already booked" without having its audio cancelled mid-sentence.
_SUCCESS_BOOKING_CLAIM = re.compile(
    r"(?:"
    r"\byou(?:'| a)?re all set\b|"
    r"\byou are all set\b|"
    r"\b(?:your|the) appointment (?:is|has been) (?:confirmed|booked|scheduled|reserved)\b|"
    r"\b(?:i(?:'| a)?ve|we(?:'| a)?ve) got you (?:down|booked|scheduled|reserved)\b|"
    r"\b(?:you(?:'| a)?re|you are) (?:booked|scheduled|confirmed)\b|"
    r"\b(?:i(?:'| a)?ve|we(?:'| a)?ve) (?:put|placed|added) you (?:on|in) (?:the )?(?:calendar|schedule)\b|"
    r"\breservation confirmed\b|"
    r"\blocked in\b"
    r")",
    re.IGNORECASE,
)

_UNAVAILABILITY_BOOKING_CONTEXT = re.compile(
    r"\b(?:already|not|no longer|isn't|is not|wasn't|cannot|can't|couldn't|"
    r"unavailable|not available)\b.{0,48}\b(?:booked|confirmed|scheduled|reserved)\b|"
    r"\b(?:booked|confirmed|scheduled|reserved)\b.{0,24}\b(?:already|taken|unavailable)\b",
    re.IGNORECASE,
)


def looks_like_unverified_booking_success(reply: str) -> bool:
    """True when the agent claims the appointment was written, not merely checked."""
    if not reply or not _SUCCESS_BOOKING_CLAIM.search(reply):
        return False
    if _UNAVAILABILITY_BOOKING_CONTEXT.search(reply):
        return False
    return True
