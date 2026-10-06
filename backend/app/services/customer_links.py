"""Remember which provider customer belongs to which local contact.

Every read and write includes the spa. A Square customer id at spa A is not
the same relationship as that same id at spa B.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.external_customer_link import ExternalCustomerLink

logger = logging.getLogger(__name__)

# Scheduling-only providers have no customer directory. A booking id is not
# a customer id, so it must never be stored here.
_NO_CUSTOMER_DIRECTORY = frozenset({"google_calendar", "internal", "unconfigured"})


def external_customer_link_statement(
    tenant_id: uuid.UUID,
    provider: str,
    external_customer_id: str,
):
    """The only supported lookup: this spa, this provider, this external id."""
    return select(ExternalCustomerLink).where(
        ExternalCustomerLink.tenant_id == tenant_id,
        ExternalCustomerLink.provider == provider,
        ExternalCustomerLink.external_customer_id == external_customer_id,
    )


def contact_provider_link_statement(
    tenant_id: uuid.UUID,
    contact_id: uuid.UUID,
    provider: str,
):
    return select(ExternalCustomerLink).where(
        ExternalCustomerLink.tenant_id == tenant_id,
        ExternalCustomerLink.contact_id == contact_id,
        ExternalCustomerLink.provider == provider,
    )


async def upsert_external_customer_link(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID | None,
    contact_id: uuid.UUID | None,
    provider: str | None,
    external_customer_id: str | None,
) -> ExternalCustomerLink | None:
    """Record a resolved provider customer for this spa.

    Does not move a link onto a different contact, and does not point a
    contact at a different provider customer. Those are identity conflicts,
    not something to guess through.
    """
    if (
        tenant_id is None
        or contact_id is None
        or not provider
        or provider in _NO_CUSTOMER_DIRECTORY
        or not external_customer_id
    ):
        return None

    now = datetime.now(timezone.utc)
    if not hasattr(db, "execute"):
        link = ExternalCustomerLink(
            tenant_id=tenant_id,
            contact_id=contact_id,
            provider=provider,
            external_customer_id=external_customer_id,
            last_verified_at=now,
        )
        add = getattr(db, "add", None)
        if callable(add):
            add(link)
        return link

    by_external = (
        await db.execute(
            external_customer_link_statement(tenant_id, provider, external_customer_id)
        )
    ).scalar_one_or_none()
    if isinstance(by_external, ExternalCustomerLink):
        if by_external.contact_id != contact_id:
            logger.warning(
                "External customer %s/%s already linked to a different contact in tenant %s",
                provider,
                external_customer_id,
                tenant_id,
            )
            return by_external
        by_external.last_verified_at = now
        return by_external

    by_contact = (
        await db.execute(
            contact_provider_link_statement(tenant_id, contact_id, provider)
        )
    ).scalar_one_or_none()
    if isinstance(by_contact, ExternalCustomerLink):
        if by_contact.external_customer_id != external_customer_id:
            logger.warning(
                "Contact %s in tenant %s is already linked to a different %s customer",
                contact_id,
                tenant_id,
                provider,
            )
            return by_contact
        by_contact.last_verified_at = now
        return by_contact

    link = ExternalCustomerLink(
        tenant_id=tenant_id,
        contact_id=contact_id,
        provider=provider,
        external_customer_id=external_customer_id,
        last_verified_at=now,
    )
    db.add(link)
    return link
