import base64
import binascii
import hashlib
import hmac
import logging
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.request_validator import RequestValidator

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.security import decode_token
from app.core.tenancy import TenantScope
from app.models import SpaAccount, User, UserRole
from app.services.call_state import CallStateStore
from app.services.token_blocklist import TokenBlocklist

logger = logging.getLogger(__name__)

_validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/login", auto_error=False
)


# --------------------------------------------------------------------------- #
# Twilio webhook security
# --------------------------------------------------------------------------- #
async def verify_twilio_signature(request: Request) -> None:
    if not settings.TWILIO_VALIDATE_SIGNATURE:
        if settings.APP_ENV == "production":
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Signature validation must be enabled in production.",
            )
        return

    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Missing Twilio signature")

    url = f"{settings.PUBLIC_BASE_URL}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    form = await request.form()
    params = {k: v for k, v in form.items() if isinstance(v, str)}

    if not _validator.validate(url, params, signature):
        logger.warning("Twilio signature validation FAILED for %s", url)
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Invalid Twilio signature")


# --------------------------------------------------------------------------- #
# xAI voice webhook security (Standard Webhooks)
# --------------------------------------------------------------------------- #
def _xai_signing_key(secret: str) -> bytes:
    """The HMAC key behind a Standard Webhooks `whsec_<base64>` secret.

    Falls back to the raw bytes when the value isn't base64, so a secret pasted
    without the prefix (or in a non-standard encoding) still verifies rather
    than silently rejecting every delivery.
    """
    raw = secret[len("whsec_") :] if secret.startswith("whsec_") else secret
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError):
        return raw.encode()


async def verify_xai_voice_signature(request: Request) -> None:
    """Authenticate a `realtime.call.incoming` delivery from xAI.

    Signed payload is `{webhook-id}.{webhook-timestamp}.{raw body}`, HMAC-SHA256
    under the signing secret returned when the number was registered. Reading
    the body here is safe: Starlette caches it, so the handler still sees it.
    """
    secret = settings.XAI_VOICE_WEBHOOK_SECRET
    if not secret:
        # Unsigned webhooks would let anyone start a voice session on our
        # account, so this is fatal in production and a loud warning in dev.
        if settings.APP_ENV == "production":
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="XAI_VOICE_WEBHOOK_SECRET must be set in production.",
            )
        logger.warning(
            "xAI voice webhook signature check SKIPPED: no XAI_VOICE_WEBHOOK_SECRET set."
        )
        return

    webhook_id = request.headers.get("webhook-id", "")
    timestamp = request.headers.get("webhook-timestamp", "")
    signature_header = request.headers.get("webhook-signature", "")
    if not (webhook_id and timestamp and signature_header):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Missing xAI webhook signature headers"
        )

    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Malformed webhook-timestamp")
    if age > settings.XAI_WEBHOOK_TOLERANCE_SECONDS:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Webhook timestamp outside tolerance window"
        )

    body = await request.body()
    signed = b".".join([webhook_id.encode(), timestamp.encode(), body])
    expected = base64.b64encode(
        hmac.new(_xai_signing_key(secret), signed, hashlib.sha256).digest()
    ).decode()

    # The header carries one or more space-separated `v1,<base64>` pairs; during
    # a secret rotation more than one is present and any may match.
    presented = [
        part.split(",", 1)[1]
        for part in signature_header.split()
        if part.startswith("v1,")
    ]
    if not any(hmac.compare_digest(expected, candidate) for candidate in presented):
        logger.warning("xAI webhook signature validation FAILED for id=%s", webhook_id)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, detail="Invalid xAI webhook signature"
        )


# --------------------------------------------------------------------------- #
# JWT authentication
# --------------------------------------------------------------------------- #
async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise unauthorized

    try:
        claims = decode_token(token)
        if claims.get("type") != "access":
            raise unauthorized
        user_id = claims["sub"]
        jti = claims["jti"]
    except (JWTError, KeyError):
        raise unauthorized

    if await TokenBlocklist(redis).is_revoked(jti):
        raise unauthorized

    user = await db.get(User, uuid.UUID(user_id))
    if user is None or not user.is_active:
        raise unauthorized

    return user


async def get_current_active_user(user: User = Depends(get_current_user)) -> User:
    return user


async def get_optional_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> User | None:
    """Resolve the caller if they presented a token, otherwise None.

    Used by `/auth/register`, which is anonymous exactly once — to create the
    very first super admin — and super-admin-only from then on.
    """
    if not token:
        return None
    try:
        return await get_current_user(token=token, db=db, redis=redis)
    except HTTPException:
        return None


# --------------------------------------------------------------------------- #
# Role-based access control
# --------------------------------------------------------------------------- #
def require_roles(*roles: UserRole) -> Callable[..., Awaitable[User]]:
    """Dependency factory gating an endpoint on the caller's role.

    Returns 403, not 404: the caller is authenticated, they just aren't allowed
    here. Used to fence the entire 6DM Sales Agent surface (leads, outbound
    campaigns, sales analytics, Dominic's calendar) off from spa tenants.
    """
    allowed = frozenset(roles)

    async def _dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail=(
                    "This resource requires one of the following roles: "
                    + ", ".join(sorted(role.value for role in allowed))
                ),
            )
        return user

    return _dependency


get_super_admin = require_roles(UserRole.SUPER_ADMIN)
get_spa_member = require_roles(UserRole.SPA_ADMIN, UserRole.SPA_STAFF)
get_tenant_manager = require_roles(UserRole.SUPER_ADMIN, UserRole.SPA_ADMIN)


async def get_current_superuser(user: User = Depends(get_super_admin)) -> User:
    """Backwards-compatible alias; `is_superuser` is now derived from the role."""
    return user


# --------------------------------------------------------------------------- #
# Tenant scoping
# --------------------------------------------------------------------------- #
async def get_tenant_scope(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    x_tenant_id: uuid.UUID | None = Header(
        None,
        alias="X-Tenant-Id",
        description=(
            "Super admin only: inspect a spa account through the tenant "
            "switcher. Ignored for everyone else."
        ),
    ),
) -> TenantScope:
    """Resolve which rows this request may read and write.

    A spa user is pinned to their own `tenant_id` and cannot widen it — passing
    someone else's id in the header is a 403, not a silently ignored hint. A
    super admin defaults to the 6DM sales workspace and opts into a tenant.
    """
    if user.role != UserRole.SUPER_ADMIN:
        if user.tenant_id is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="Account is not assigned to a spa. Contact your administrator.",
            )
        if x_tenant_id is not None and x_tenant_id != user.tenant_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail="You may only access your own spa account.",
            )
        return TenantScope.for_tenant(user.tenant_id)

    if x_tenant_id is None:
        return TenantScope.for_sales_workspace(user.id)

    spa = await db.get(SpaAccount, x_tenant_id)
    if spa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")
    return TenantScope.for_tenant(spa.id)


def get_call_state_store(redis: Redis = Depends(get_redis)) -> CallStateStore:
    return CallStateStore(redis)


__all__ = [
    "verify_twilio_signature",
    "verify_xai_voice_signature",
    "get_current_user",
    "get_current_active_user",
    "get_optional_current_user",
    "get_current_superuser",
    "require_roles",
    "get_super_admin",
    "get_spa_member",
    "get_tenant_manager",
    "get_tenant_scope",
    "get_call_state_store",
    "get_db",
    "get_redis",
]