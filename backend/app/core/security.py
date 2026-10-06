import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from jose import jwt
from passlib.context import CryptContext

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

TokenType = Literal["access", "refresh"]


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": str(uuid.uuid4()),
        **(extra_claims or {}),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(
    subject: str, extra_claims: dict[str, Any] | None = None
) -> str:
    """`extra_claims` carries `role`/`tenant_id` so the dashboard can pick its
    navigation without a round trip. Authorization is still decided server-side
    from the User row — these claims are a UI hint, never a permission grant."""
    return _create_token(
        subject,
        "access",
        timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        extra_claims,
    )


def create_refresh_token(subject: str) -> str:
    return _create_token(
        subject, "refresh", timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )


def decode_token(token: str) -> dict[str, Any]:
    """Raises jose.JWTError on invalid/expired tokens."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])