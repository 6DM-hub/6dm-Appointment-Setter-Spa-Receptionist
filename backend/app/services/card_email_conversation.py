"""Deterministic email read-back before card-link permission; no model consent."""
import re
from app.services.card_email import email_address
from app.services.booking_state import get_draft, save_draft, is_affirmative, is_negative, utterance_modifies_booking

EMAIL_QUESTION = "What email address would you like me to use for your secure card link?"


def confirmed_email(session):
    value = session.entities.get("card_email_confirmed") or {}
    if value.get("revision") == get_draft(session).draft_revision:
        return email_address(value.get("email"))
    return None


def begin_email(session):
    session.entities["card_email_collecting"] = {"revision": get_draft(session).draft_revision}
    return EMAIL_QUESTION


def email_answer(session, text):
    """Returns (handled, line, ready). A yes only confirms the exact read-back."""
    pending = session.entities.get("card_email_collecting")
    if not pending:
        return False, None, False
    draft = get_draft(session)
    compact = re.sub(r"^(?:my email is|it is|it's)\s+", "", text.strip().lower())
    compact = re.sub(r"\s+at\s+", "@", compact)
    compact = re.sub(r"\s+dot\s+", ".", compact)
    compact = re.sub(r"\s+", "", compact).strip(".,!?")
    address = email_address(compact)
    if pending.get("revision") != draft.draft_revision or (not address and utterance_modifies_booking(text)):
        session.entities.pop("card_email_collecting", None)
        return False, None, False
    if pending.get("email") and is_affirmative(text):
        session.entities["card_email_confirmed"] = dict(pending)
        session.entities.pop("card_email_collecting", None)
        draft.caller_email = pending["email"]
        save_draft(session, draft)
        return True, None, True
    if is_negative(text):
        if not pending.get("email"):
            session.entities.pop("card_email_collecting", None)
            session.entities["callback_offer_pending"] = True
            return True, "Would you like me to request a callback from the spa team to help with this appointment?", False
        pending.pop("email", None)
        return True, "No problem. What email address should I use instead? If you prefer not to share one, I can ask the spa team to help.", False
    if address:
        pending["email"] = address
        return True, f"I heard {address}. Is that email address correct?", False
    pending["attempts"] = int(pending.get("attempts", 0)) + 1
    if pending["attempts"] >= 3:
        session.entities.pop("card_email_collecting", None)
        session.entities["callback_offer_pending"] = True
        return True, "I'm having trouble confirming that address. Would you like me to request a callback from the spa team to help?", False
    return True, "Please say or spell just your email address, including the part after the at sign.", False
