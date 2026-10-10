"""Caller acceptance of a provider slot survives identity collection, not edits."""
from app.services.booking_state import get_draft, proposal_fingerprint


def remember_offer(session):
    draft = get_draft(session)
    if draft.provider_verified and draft.selected_slot:
        session.entities["spoken_booking_offer"] = proposal_fingerprint(draft)


def accept_offer(session):
    draft = get_draft(session)
    fingerprint = proposal_fingerprint(draft)
    if not draft.provider_verified or not draft.selected_slot:
        return False
    if session.entities.get("spoken_booking_offer") != fingerprint:
        return False
    session.entities["accepted_booking_offer"] = fingerprint
    return True


def offer_accepted(session):
    draft = get_draft(session)
    return bool(draft.provider_verified and draft.selected_slot
                and session.entities.get("accepted_booking_offer") == proposal_fingerprint(draft))


def authorize_accepted_offer(session):
    """Promote prior slot consent after required identity has been collected."""
    from app.services.booking_state import mark_read_back, record_pure_confirmation
    if not offer_accepted(session):
        return False
    mark_read_back(session)
    return record_pure_confirmation(session, "yes")
