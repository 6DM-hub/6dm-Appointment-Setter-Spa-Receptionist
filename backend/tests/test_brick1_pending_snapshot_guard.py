"""Verified requests remain pending through guard state, reconnect and call end."""
import json
from zoneinfo import ZoneInfo

import pytest

from app.services import booking_change_guard as guard
from app.services.booking_state import BookingDraft, get_draft, save_draft
from app.services.call_state import CallSession

PENDING = "awaiting_provider_approval"
TZ = ZoneInfo("America/Chicago")


def pending_session(service="Teeth Cleaning"):
    session = CallSession("CA_PENDING_GUARD", "inbound", "+15555550100", "+15555550101",
                          booking_status=PENDING, appointment_id="local-1",
                          external_booking_id="square-1", timezone=TZ.key)
    save_draft(session, BookingDraft(appointment_id="local-1", external_booking_id="square-1",
                                    service_description=service, start_iso="2026-10-22T19:00:00Z",
                                    end_iso="2026-10-22T20:00:00Z"))
    session.entities["active_appointment_id"] = "local-1"
    session.entities["last_booking_outcome"] = PENDING
    guard.record_booking_completed(session, 1)
    # The same serialization used to reconnect a call, not an in-process alias.
    return CallSession.from_dict(json.loads(json.dumps(session.to_dict())))


@pytest.mark.parametrize("service", ["Massage", "AC Tune-Up", "Teeth Cleaning"])
def test_pending_reconnect_is_exact_replay_but_a_changed_slot_is_not(service):
    session = pending_session(service)
    assert guard.is_exact_booked_replay(session)
    draft = get_draft(session)
    draft.start_iso = "2026-10-22T20:00:00Z"
    save_draft(session, draft)
    assert not guard.is_exact_booked_replay(session)


@pytest.mark.parametrize("service", ["Massage", "AC Tune-Up", "Teeth Cleaning"])
def test_keep_and_call_end_preserve_awaiting_approval(service):
    session = pending_session(service)
    session.booking_status = "collecting_details"
    draft = get_draft(session)
    draft.start_iso = "2026-10-22T20:00:00Z"
    save_draft(session, draft)
    assert guard.reported_booking_outcome(session) == PENDING
    assert guard.settle_abandoned_change(session)
    assert session.booking_status == PENDING
    assert get_draft(session).start_iso == "2026-10-22T19:00:00Z"
    speech = guard.keep_existing_booking(session, TZ, turn=3).lower()
    assert "awaiting staff approval" in speech
    assert "booked" not in speech and "confirmed" not in speech
    assert guard.reported_booking_outcome(session) == PENDING
    assert not guard.settle_abandoned_change(session)


def test_pending_cancellation_refusal_and_additional_notice_do_not_claim_confirmed():
    session = pending_session()
    speech = guard.refused_cancellation_speech(session, TZ).lower()
    assert "awaiting staff approval" in speech
    assert "booked" not in speech and "confirmed" not in speech
    guard.mark_additional_booking(session, TZ)
    notice = session.entities[guard.ADDITIONAL_BOOKING]["notice"].lower()
    assert "awaiting staff approval" in notice
    assert "stays booked" not in notice


@pytest.mark.parametrize("reason", ["cancelled", "unknown"])
def test_pending_request_is_not_restored_after_cancel_or_unknown_change(reason):
    session = pending_session()
    session.booking_status = "error"
    if reason == "cancelled":
        session.entities.pop("active_appointment_id")
        session.entities["last_booking_outcome"] = "cancelled"
    else:
        session.entities[guard.RECONCILIATION_REQUIRED] = {"operation": "move"}
    assert guard.reported_booking_outcome(session) == "error"
    assert not guard.settle_abandoned_change(session)
