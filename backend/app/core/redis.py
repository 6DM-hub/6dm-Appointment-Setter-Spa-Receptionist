from __future__ import annotations

import logging

import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)


class RedisManager:
    def __init__(self) -> None:
        self._client: redis.Redis | None = None

    @property
    def client(self) -> redis.Redis:
        if self._client is None:
            logger.info(
                "Redis connection source: %s",
                "REDIS_URL" if settings.redis_uses_connection_url else "REDIS_HOST/REDIS_PORT",
            )
            self._client = redis.from_url(
                settings.REDIS_URL,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=5,
            )
        return self._client

    async def ping(self) -> bool:
        try:
            return await self.client.ping()
        except Exception as exc:
            logger.warning("Redis ping failed: %s", exc)
            return False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


redis_manager = RedisManager()


# FastAPI Lifespan Helper Functions required by main.py
async def init_redis() -> redis.Redis:
    """Initialize Redis and verify the connection."""
    client = redis_manager.client

    is_connected = await redis_manager.ping()

    if is_connected:
        logger.info("✅ Redis connection established successfully.")
    else:
        logger.warning("⚠️ Redis ping failed on startup.")

    return client


async def close_redis() -> None:
    """Close Redis connection pool on shutdown."""
    await redis_manager.close()
    logger.info("🛑 Redis connection closed.")


async def get_redis() -> redis.Redis:
    """Dependency helper to retrieve the active Redis instance."""
    return redis_manager.client