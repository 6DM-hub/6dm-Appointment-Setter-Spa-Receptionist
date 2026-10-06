import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from jose import JWTError
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_active_user,
    get_db,
    get_optional_current_user,
    get_redis,
)
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models import SpaAccount, User, UserRole
from app.schemas.auth import (
    RefreshRequest,
    TokenPair,
    UserLogin,
    UserRead,
    UserRegister,
    UserUpdate,
)
from app.services.token_blocklist import TokenBlocklist

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_tokens(user: User) -> TokenPair:
    """Access tokens carry `role`/`tenant_id` so the dashboard can choose its
    navigation on first paint. Every endpoint still authorizes from the User
    row — these claims decide what is *shown*, never what is *allowed*."""
    return TokenPair(
        access_token=create_access_token(
            str(user.id),
            {
                "role": user.role.value,
                "tenant_id": str(user.tenant_id) if user.tenant_id else None,
            },
        ),
        refresh_token=create_refresh_token(str(user.id)),
    )


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(
    payload: UserRegister,
    db: AsyncSession = Depends(get_db),
    actor: User | None = Depends(get_optional_current_user),
) -> User:
    """Provision an account.

    Open only while the database has no users, so a fresh deployment can create
    its first super admin. After that it is a super-admin tool: spa tenants are
    onboarded by 6DM, and self-service signup would otherwise let anyone mint a
    `super_admin` and read every tenant's transcripts.
    """
    user_count = (await db.execute(select(func.count(User.id)))).scalar_one()
    is_bootstrap = user_count == 0

    if is_bootstrap:
        role, tenant_id = UserRole.SUPER_ADMIN, None
    else:
        if actor is None or actor.role != UserRole.SUPER_ADMIN:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "Only a super admin can provision accounts.",
            )
        role, tenant_id = payload.role, payload.tenant_id

    if tenant_id is not None and await db.get(SpaAccount, tenant_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")

    existing = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        role=role,
        tenant_id=tenant_id,
        is_superuser=role == UserRole.SUPER_ADMIN,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
async def login(payload: UserLogin, db: AsyncSession = Depends(get_db)) -> TokenPair:
    user = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Account disabled")

    return _issue_tokens(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh_tokens(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> TokenPair:
    blocklist = TokenBlocklist(redis)
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")

    try:
        claims = decode_token(payload.refresh_token)
    except JWTError:
        raise unauthorized

    if claims.get("type") != "refresh":
        raise unauthorized

    jti = claims.get("jti")
    if not jti or await blocklist.is_revoked(jti):
        raise unauthorized

    user = await db.get(User, uuid.UUID(claims["sub"]))
    if not user or not user.is_active:
        raise unauthorized

    # Rotate: revoke the used refresh token, issue a fresh pair
    remaining_ttl = max(int(claims["exp"] - datetime.now(timezone.utc).timestamp()), 1)
    await blocklist.revoke(jti, remaining_ttl)

    # Re-read from the row rather than copying the old claims, so a role or
    # tenant change takes effect on the next refresh.
    return _issue_tokens(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshRequest, redis: Redis = Depends(get_redis)) -> None:
    blocklist = TokenBlocklist(redis)
    try:
        claims = decode_token(payload.refresh_token)
        jti = claims.get("jti")
        if jti:
            remaining_ttl = max(int(claims["exp"] - datetime.now(timezone.utc).timestamp()), 1)
            await blocklist.revoke(jti, remaining_ttl)
    except JWTError:
        pass  # already invalid


@router.get("/me", response_model=UserRead)
async def get_me(current_user: User = Depends(get_current_active_user)) -> User:
    return current_user


@router.patch("/me", response_model=UserRead)
async def update_me(
    payload: UserUpdate,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(current_user, field, value)
    await db.commit()
    await db.refresh(current_user)
    return current_user
