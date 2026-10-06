import asyncio
import logging

from fastapi import APIRouter
from app.core.database import check_db_connection
from app.core.redis import redis_manager
from app.core.config import settings
from app.services.grok_service import grok_service
from app.services.twilio_service import twilio_service

router = APIRouter(tags=["Health"])
logger = logging.getLogger(__name__)

@router.get("/health")
async def health_check():
    db_ok = await check_db_connection()
    redis_ok = await redis_manager.ping()
    overall_status = "healthy" if db_ok and redis_ok else "degraded"
    return {
        "status": overall_status,
        "services": {
            "api": "healthy",
            "postgres": "healthy" if db_ok else "unreachable",
            "redis": "healthy" if redis_ok else "unreachable",
        },
    }


async def _check_twilio() -> str:
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return "not_configured"
    try:
        await asyncio.to_thread(twilio_service._client.api.accounts(settings.TWILIO_ACCOUNT_SID).fetch)
        return "connected"
    except Exception:
        logger.warning("Twilio provider health check failed", exc_info=True)
        return "disconnected"


async def _check_grok() -> str:
    if not settings.XAI_API_KEY:
        return "not_configured"
    try:
        response = await grok_service._client.get("/models")
        response.raise_for_status()
        return "connected"
    except Exception:
        logger.warning("Grok provider health check failed", exc_info=True)
        return "disconnected"


@router.get("/health/providers")
async def provider_health_check():
    twilio, grok = await asyncio.gather(_check_twilio(), _check_grok())
    return {"twilio": twilio, "grok": grok}
