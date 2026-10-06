"""Persist caller identity without crossing tenant boundaries."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import scope_filter
from app.models import CallLog, Contact
from app.services.call_state import CallSession
from app.services.phone_numbers import canonical_customer_phone, is_non_phone_caller_id


async def persist_caller_identity(
    db: AsyncSession,
    session: CallSession,
    caller_name: str | None,
    caller_email: str | None = None,
) -> Contact | None:
    """Create/update the scoped contact and link the current call log.

    Existing non-empty names are preserved because model extraction can be
    uncertain. The unique tenant/phone index prevents duplicate contacts.
    """
    scope = session.scope
    phone = canonical_customer_phone(
        session.customer_phone, timezone_name=session.timezone
    )
    if scope is None or is_non_phone_caller_id(session.customer_phone) or not phone:
        return None

    contact = (
        await db.execute(
            select(Contact).where(
                scope_filter(scope, Contact), Contact.phone_number == phone
            )
        )
    ).scalar_one_or_none()

    clean_name = " ".join((caller_name or "").split()) or None
    if contact is None and not clean_name and not caller_email:
        return None
    if contact is None:
        parts = clean_name.split(maxsplit=1) if clean_name else []
        contact = Contact(
            phone_number=phone,
            first_name=parts[0] if parts else None,
            last_name=parts[1] if len(parts) > 1 else None,
            email=caller_email,
            **({"tenant_id": scope.tenant_id} if scope.tenant_id else {"owner_id": scope.owner_id}),
        )
        db.add(contact)
        await db.flush()
    else:
        if clean_name and not contact.first_name:
            parts = clean_name.split(maxsplit=1)
            contact.first_name = parts[0]
            contact.last_name = parts[1] if len(parts) > 1 else contact.last_name
        if caller_email and not contact.email:
            contact.email = caller_email

    call_log = (
        await db.execute(
            select(CallLog).where(CallLog.twilio_call_sid == session.call_sid)
        )
    ).scalar_one_or_none()
    if call_log and call_log.contact_id is None:
        call_log.contact_id = contact.id
    session.entities["contact_id"] = str(contact.id)
    return contact