"""Spa tenant provisioning and per-spa receptionist configuration.

Onboarding is one POST here: name, Twilio number, Grok prompt, services, staff,
hours and booking provider. The inbound webhook resolves everything else from
that row at call time, so no deploy is involved.

Who can do what:
  * super_admin — create, list, read and edit any spa; hard-delete is not
    offered (deactivate instead, so call history survives).
  * spa_admin   — read and edit their own spa's receptionist settings.
  * spa_staff   — read their own spa only.
"""
import logging
import json
import uuid
import secrets
from datetime import datetime, timezone
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from redis.asyncio import Redis

from app.api.deps import get_current_user, get_db, get_redis, get_super_admin, get_tenant_manager
from app.core.config import settings
from app.models import BookingProvider, SpaAccount, User, UserRole
from app.models.google_calendar_connection import GoogleCalendarConnection
from app.schemas import (
    Page,
    SpaAccountCreate,
    SpaAccountRead,
    SpaAccountSummary,
    SpaAccountUpdate,
)
from app.services.booking_config import (
    decrypt_config,
    encrypt_config,
    masked_config,
    merge_config,
    missing_config,
    validate_config,
)
from app.services.booking_adapters.providers import VERTICAL_PROVIDERS
from app.services.google_calendar import (
    GoogleCalendarError,
    authorization_url,
    exchange_code,
    list_calendars,
    test_connection as google_test_connection,
)

router = APIRouter(prefix="/spa-accounts", tags=["spa-accounts"])
logger = logging.getLogger(__name__)

# Fields a spa_admin may not change on their own tenant. Moving your own
# inbound number would let you hijack another spa's routing.
TENANT_LOCKED_FIELDS = {"twilio_phone_number", "is_active", "voice_engine"}


class GoogleCalendarSelection(BaseModel):
    calendar_id: str


def _google_redirect(result: str, detail: str | None = None) -> RedirectResponse:
    params = {"google_calendar": result}
    if detail:
        params["reason"] = detail
    url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/spa/settings?{urlencode(params)}"
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _connection_read(connection: GoogleCalendarConnection | None) -> dict:
    if connection is None:
        return {
            "status": "not_configured",
            "connected": False,
            "google_account_email": None,
            "selected_calendar_id": None,
            "last_tested_at": None,
        }
    return {
        "status": connection.status,
        "connected": True,
        "google_account_email": connection.google_account_email,
        "selected_calendar_id": connection.selected_calendar_id,
        "last_tested_at": connection.last_tested_at,
    }


def _to_read(spa: SpaAccount) -> SpaAccountRead:
    """`booking_config` holds provider secrets, so the read schema reports only
    whether the configured provider is actually usable."""
    configured = not missing_config(spa.booking_provider, spa.booking_config)
    return SpaAccountRead.model_validate(spa).model_copy(
        update={
            "booking_config": masked_config(spa.booking_provider, spa.booking_config),
            "booking_provider_configured": configured,
        }
    )


async def _assert_number_free(
    db: AsyncSession, number: str | None, exclude_id: uuid.UUID | None = None
) -> None:
    if not number:
        return
    query = select(SpaAccount.id).where(SpaAccount.twilio_phone_number == number)
    if exclude_id:
        query = query.where(SpaAccount.id != exclude_id)
    if (await db.execute(query)).scalar_one_or_none():
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{number} is already routed to another spa account.",
        )


async def _load_visible_spa(
    db: AsyncSession, spa_id: uuid.UUID, user: User
) -> SpaAccount:
    if user.role != UserRole.SUPER_ADMIN and spa_id != user.tenant_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")
    spa = await db.get(SpaAccount, spa_id)
    if spa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")
    return spa


@router.post(
    "",
    response_model=SpaAccountRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_super_admin)],
)
async def create_spa_account(
    payload: SpaAccountCreate,
    db: AsyncSession = Depends(get_db),
) -> SpaAccountRead:
    await _assert_number_free(db, payload.twilio_phone_number)

    # Python mode, not JSON: nested models still flatten to plain dicts for the
    # JSONB columns, but `booking_provider` stays an enum member, which is what
    # the SQLAlchemy Enum column expects.
    values = payload.model_dump()
    values["booking_config"] = encrypt_config(
        validate_config(values["booking_provider"], values.get("booking_config"))
    )
    spa = SpaAccount(**values)
    db.add(spa)
    await db.commit()
    await db.refresh(spa)
    return _to_read(spa)


@router.get(
    "",
    response_model=Page[SpaAccountSummary],
    dependencies=[Depends(get_super_admin)],
)
async def list_spa_accounts(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
) -> Page[SpaAccountSummary]:
    """Backs the super admin's tenant switcher."""
    query = select(SpaAccount)
    if not include_inactive:
        query = query.where(SpaAccount.is_active.is_(True))

    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            query.order_by(SpaAccount.name.asc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return Page(
        items=[SpaAccountSummary.model_validate(r) for r in rows],
        total=total, page=page, size=size,
    )


@router.get("/me", response_model=SpaAccountRead)
async def get_my_spa_account(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SpaAccountRead:
    """The signed-in spa user's own tenant — what the spa dashboard loads."""
    if current_user.tenant_id is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This account is not attached to a spa. Super admins should pick "
            "one from the tenant switcher instead.",
        )
    spa = await db.get(SpaAccount, current_user.tenant_id)
    if spa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")
    return _to_read(spa)


@router.get("/booking/google/callback")
async def google_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> RedirectResponse:
    if error or not code or not state:
        return _google_redirect("authorization_required")
    raw_state = await redis.getdel(f"google-oauth-state:{state}")
    if not raw_state:
        return _google_redirect("authorization_required", "invalid_state")
    try:
        state_value = raw_state.decode() if isinstance(raw_state, bytes) else raw_state
        state_payload = json.loads(state_value)
        spa_id = uuid.UUID(state_payload["spa_id"])
        user_id = uuid.UUID(state_payload["user_id"])
    except (ValueError, UnicodeDecodeError, KeyError, TypeError, json.JSONDecodeError):
        return _google_redirect("authorization_required", "invalid_state")
    user = await db.get(User, user_id)
    if user is None or not user.is_active or (
        user.role != UserRole.SUPER_ADMIN and user.tenant_id != spa_id
    ):
        return _google_redirect("authorization_required", "invalid_state")
    spa = await db.get(SpaAccount, spa_id)
    if spa is None:
        return _google_redirect("authorization_required")
    try:
        code_verifier = state_payload["code_verifier"]
        credentials, email = await exchange_code(code, state, code_verifier)
    except (KeyError, TypeError, json.JSONDecodeError):
        logger.warning("Google OAuth callback rejected: PKCE verifier missing for spa %s", spa_id)
        return _google_redirect("authorization_required", "missing_verifier")
    except Exception:
        logger.exception(
            "Google OAuth token exchange failed for spa %s",
            spa_id,
        )
        return _google_redirect("authorization_required")

    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(
                GoogleCalendarConnection.spa_id == spa_id
            )
        )
    ).scalar_one_or_none()

    existing_config = decrypt_config(spa.booking_config)

    if connection is None:
        connection = GoogleCalendarConnection(spa_id=spa_id)
        db.add(connection)

    connection.google_account_email = email
    connection.credentials = encrypt_config(credentials)
    connection.scopes = list(credentials.get("scopes") or [])
    connection.selected_calendar_id = existing_config.get("google_calendar_id")
    connection.status = "connected"
    connection.last_error = None

    await db.commit()

    return _google_redirect("connected")



    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(GoogleCalendarConnection.spa_id == spa_id)
        )
    ).scalar_one_or_none()
    existing_config = decrypt_config(spa.booking_config)
    if connection is None:
        connection = GoogleCalendarConnection(spa_id=spa_id)
        db.add(connection)
    connection.google_account_email = email
    connection.credentials = encrypt_config(credentials)
    connection.scopes = list(credentials.get("scopes") or [])
    connection.selected_calendar_id = existing_config.get("google_calendar_id")
    connection.status = "connected"
    connection.last_error = None
    await db.commit()
    return _google_redirect("connected")


@router.get("/{spa_id}/enhancements/report")
async def enhancement_report(spa_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete
    from app.models.enhancement_offer import EnhancementOffer
    from app.services.enhancements import metrics
    spa = await _load_visible_spa(db, spa_id, current_user)
    days = int((spa.enhancement_settings or {}).get("retention_days", 90))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    await db.execute(delete(EnhancementOffer).where(EnhancementOffer.tenant_id == spa_id, EnhancementOffer.created_at < cutoff))
    await db.commit()
    rows = list((await db.execute(select(EnhancementOffer).where(EnhancementOffer.tenant_id == spa_id, EnhancementOffer.created_at >= cutoff))).scalars())
    return metrics(rows)

@router.post("/{spa_id}/enhancements/phrasing")
async def enhancement_phrasing(spa_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from app.services.enhancement_phrasing import generate_phrasing
    if current_user.role == UserRole.SPA_STAFF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner access required.")
    await _load_visible_spa(db, spa_id, current_user)
    if not settings.XAI_API_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Grok text access is not configured. Default verified invitations remain available.")
    try:
        return {"phrases": await generate_phrasing(), "saved": False}
    except Exception:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Grok could not produce valid invitations. Default verified invitations remain available.")

@router.delete("/{spa_id}/enhancements/history", status_code=204)
async def clear_enhancement_history(spa_id: uuid.UUID, current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from sqlalchemy import delete
    from app.models.enhancement_offer import EnhancementOffer
    if current_user.role == UserRole.SPA_STAFF:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner access required.")
    await _load_visible_spa(db, spa_id, current_user)
    await db.execute(delete(EnhancementOffer).where(EnhancementOffer.tenant_id == spa_id))
    await db.commit()

@router.get("/{spa_id}", response_model=SpaAccountRead)
async def get_spa_account(
    spa_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SpaAccountRead:
    return _to_read(await _load_visible_spa(db, spa_id, current_user))


@router.patch("/{spa_id}", response_model=SpaAccountRead)
async def update_spa_account(
    spa_id: uuid.UUID,
    payload: SpaAccountUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SpaAccountRead:
    if current_user.role == UserRole.SPA_STAFF:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "spa_staff accounts have read-only access to receptionist settings.",
        )

    spa = await _load_visible_spa(db, spa_id, current_user)
    updates = payload.model_dump(exclude_unset=True)

    if current_user.role != UserRole.SUPER_ADMIN:
        locked = TENANT_LOCKED_FIELDS & updates.keys()
        if locked:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"Only 6DM can change: {', '.join(sorted(locked))}.",
            )

    if "twilio_phone_number" in updates:
        await _assert_number_free(db, updates["twilio_phone_number"], exclude_id=spa.id)

    if "booking_provider" in updates or "booking_config" in updates:
        provider = updates.get("booking_provider", spa.booking_provider)
        existing = {} if provider != spa.booking_provider else spa.booking_config
        updates["booking_config"] = encrypt_config(
            merge_config(provider, existing, updates.get("booking_config", {}))
        )
    for field, value in updates.items():
        setattr(spa, field, value)
    await db.commit()
    await db.refresh(spa)
    return _to_read(spa)


@router.get("/{spa_id}/booking/google/connect")
async def connect_google_calendar(
    spa_id: uuid.UUID,
    current_user: User = Depends(get_tenant_manager),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    await _load_visible_spa(db, spa_id, current_user)
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google OAuth is not configured")
    state = secrets.token_urlsafe(32)
    authorization, code_verifier = authorization_url(state)
    await redis.setex(
        f"google-oauth-state:{state}",
        settings.GOOGLE_OAUTH_STATE_TTL_SECONDS,
        json.dumps(
            {
                "spa_id": str(spa_id),
                "user_id": str(current_user.id),
                "code_verifier": code_verifier,
            },
            separators=(",", ":"),
        ),
    )
    return {"authorization_url": authorization}


@router.get("/{spa_id}/booking/google/status")
async def google_calendar_status(
    spa_id: uuid.UUID,
    current_user: User = Depends(get_tenant_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    spa = await _load_visible_spa(db, spa_id, current_user)
    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(GoogleCalendarConnection.spa_id == spa.id)
        )
    ).scalar_one_or_none()
    return _connection_read(connection)


@router.get("/{spa_id}/booking/google/calendars")
async def google_calendars(
    spa_id: uuid.UUID,
    current_user: User = Depends(get_tenant_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    spa = await _load_visible_spa(db, spa_id, current_user)
    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(GoogleCalendarConnection.spa_id == spa.id)
        )
    ).scalar_one_or_none()
    if connection is None:
        return {"status": "authorization_required", "calendars": []}
    try:
        calendars = await list_calendars(connection)
    except GoogleCalendarError as exc:
        connection.status = exc.status
        connection.last_error = exc.status
        await db.commit()
        return {"status": exc.status, "calendars": []}
    return {"status": "connected", "calendars": calendars}


@router.put("/{spa_id}/booking/google/calendar")
async def select_google_calendar(
    spa_id: uuid.UUID,
    payload: GoogleCalendarSelection,
    current_user: User = Depends(get_tenant_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    spa = await _load_visible_spa(db, spa_id, current_user)
    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(GoogleCalendarConnection.spa_id == spa.id)
        )
    ).scalar_one_or_none()
    if connection is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Connect Google Calendar first")
    try:
        calendars = await list_calendars(connection)
    except GoogleCalendarError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, exc.status) from exc
    if not any(item["id"] == payload.calendar_id for item in calendars):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Calendar is not accessible to this Google account")
    connection.selected_calendar_id = payload.calendar_id
    current = decrypt_config(spa.booking_config)
    current["google_calendar_id"] = payload.calendar_id
    spa.booking_config = encrypt_config(validate_config(BookingProvider.GOOGLE_CALENDAR, current))
    await db.commit()
    return {"status": "connected", "selected_calendar_id": payload.calendar_id}


@router.post("/{spa_id}/booking/google/disconnect")
async def disconnect_google_calendar(
    spa_id: uuid.UUID,
    current_user: User = Depends(get_tenant_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    spa = await _load_visible_spa(db, spa_id, current_user)
    connection = (
        await db.execute(
            select(GoogleCalendarConnection).where(GoogleCalendarConnection.spa_id == spa.id)
        )
    ).scalar_one_or_none()
    if connection is not None:
        await db.delete(connection)
    current = decrypt_config(spa.booking_config)
    current.pop("google_calendar_id", None)
    spa.booking_config = encrypt_config(validate_config(BookingProvider.GOOGLE_CALENDAR, current))
    await db.commit()
    return {"status": "not_configured"}


@router.post("/{spa_id}/booking/test")
async def test_booking_connection(
    spa_id: uuid.UUID,
    current_user: User = Depends(get_tenant_manager),
    db: AsyncSession = Depends(get_db),
) -> dict:
    spa = await _load_visible_spa(db, spa_id, current_user)
    missing = missing_config(spa.booking_provider, spa.booking_config)
    if spa.booking_provider is BookingProvider.GOOGLE_CALENDAR:
        connection = (
            await db.execute(
                select(GoogleCalendarConnection).where(GoogleCalendarConnection.spa_id == spa.id)
            )
        ).scalar_one_or_none()
        if connection is None:
            return {"status": "authorization_required", "missing": []}
        if not connection.selected_calendar_id:
            return {"status": "calendar_not_selected", "missing": []}
        try:
            await google_test_connection(connection)
        except GoogleCalendarError as exc:
            connection.status = exc.status
            connection.last_error = exc.status
            connection.last_tested_at = datetime.now(timezone.utc)
            await db.commit()
            return {"status": exc.status, "missing": []}
        connection.status = "connected"
        connection.last_error = None
        connection.last_tested_at = datetime.now(timezone.utc)
        await db.commit()
        return {"status": "connected", "missing": []}
    if missing:
        return {"status": "not_configured", "missing": missing}
    provider_cls = VERTICAL_PROVIDERS.get(spa.booking_provider)
    if provider_cls is not None and not provider_cls.implemented:
        return {"status": "needs_attention", "missing": []}
    return {"status": "connected", "missing": []}
