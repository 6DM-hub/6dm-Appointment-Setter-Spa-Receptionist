"""Redis-backed revocation list for rotated/logged-out refresh tokens."""
from redis.asyncio import Redis


class TokenBlocklist:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    def _key(self, jti: str) -> str:
        return f"token:revoked:{jti}"

    async def revoke(self, jti: str, ttl_seconds: int) -> None:
        await self._redis.set(self._key(jti), b"1", ex=max(ttl_seconds, 1))

    async def is_revoked(self, jti: str) -> bool:
        return bool(await self._redis.exists(self._key(jti)))