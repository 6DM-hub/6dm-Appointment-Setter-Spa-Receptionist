"""Phase 1 regression: a caller's NAME must never become a therapist
preference.

Reported bug: the realtime model asked "May I have your full name for the
appointment?", the caller answered "Green", and the tool call that followed
carried `caller_name="Green"` AND `preferred_staff="Green"` — the model had
copied the same answer into both fields. `preferred_staff` must only be set
from an explicit therapist request ("can I book with Sarah"), never from an
answer to a name question.

`booking_state._was_answer_to_caller_name_question` (used by `stage()`) is the
code-level guard for this — it does not trust the model's own field
separation, it re-derives the answer from the actual transcript: if the value
being proposed as `preferred_staff` is exactly what the caller said in direct
response to a name question, it is customer identity only.
"""
from app.services.booking_state import get_draft, stage
from app.services.call_state import CallSession
from app.services.grok_service import AppointmentIntent


def _session_after_name_question(answer: str) -> CallSession:
    session = CallSession(
        "call-green", "inbound", "+15550000002", "+15550000001", tenant_id="tenant-1"
    )
    session.add_turn("assistant", "May I have your full name for the appointment?")
    session.add_turn("user", answer)
    return session


def test_caller_name_given_as_answer_to_name_question_never_becomes_preferred_staff():
    session = _session_after_name_question("Green")

    intent = AppointmentIntent(
        intent="schedule",
        confidence=1.0,
        caller_name="Green",
        # The exact reported bug: the model also copied it into staff intent.
        preferred_staff="Green",
    )

    draft = stage(session, intent)

    assert draft.caller_name == "Green"
    assert draft.preferred_staff is None


def test_explicit_therapist_request_still_sets_preferred_staff():
    """The guard must not be so aggressive that it blocks real staff
    preferences stated in their own turn."""
    session = CallSession(
        "call-sarah", "inbound", "+15550000002", "+15550000001", tenant_id="tenant-1"
    )
    session.add_turn("assistant", "What service would you like, and any therapist preference?")
    session.add_turn("user", "Can I book with Sarah?")

    intent = AppointmentIntent(
        intent="schedule",
        confidence=1.0,
        preferred_staff="Sarah",
    )

    draft = stage(session, intent)

    assert draft.preferred_staff == "Sarah"


def test_name_and_staff_preference_given_in_separate_turns_both_survive():
    """Answering the name question with "Green" and, separately and later,
    asking for a real therapist by name must not cancel each other out."""
    session = CallSession(
        "call-both", "inbound", "+15550000002", "+15550000001", tenant_id="tenant-1"
    )
    session.add_turn("assistant", "May I have your full name for the appointment?")
    session.add_turn("user", "Green")
    draft = stage(
        session,
        AppointmentIntent(intent="schedule", confidence=1.0, caller_name="Green", preferred_staff="Green"),
    )
    assert draft.caller_name == "Green"
    assert draft.preferred_staff is None

    session.add_turn("assistant", "Any therapist preference?")
    session.add_turn("user", "I'd like Sarah please.")
    draft = stage(
        session,
        AppointmentIntent(intent="schedule", confidence=1.0, preferred_staff="Sarah"),
    )

    assert draft.caller_name == "Green"
    assert draft.preferred_staff == "Sarah"
