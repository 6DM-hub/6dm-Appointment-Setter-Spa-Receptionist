"""Daily retention cleanup, including businesses that disabled recommendations."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, delete
from app.core.database import AsyncSessionLocal
from app.models import SpaAccount, EnhancementOffer

async def purge_expired(db, now=None):
    now = now or datetime.now(timezone.utc)
    policies = (await db.execute(select(SpaAccount.id, SpaAccount.enhancement_settings))).all()
    for tenant_id, config in policies:
        days = max(1, min(365, int((config or {}).get("retention_days", 90))))
        await db.execute(delete(EnhancementOffer).where(
            EnhancementOffer.tenant_id == tenant_id,
            EnhancementOffer.created_at < now - timedelta(days=days)))
    await db.commit()

async def run_retention():
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await purge_expired(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.getLogger(__name__).warning("Enhancement retention cleanup unavailable")
        await asyncio.sleep(86400)
