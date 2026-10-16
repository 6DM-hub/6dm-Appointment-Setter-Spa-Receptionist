import uuid
from datetime import datetime, timezone
from app.services.scheduling_time import utc_instant

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_tenant_scope
from app.core.tenancy import TenantScope, owns, scope_columns, scope_filter
from app.models import Appointment, AppointmentStatus, Contact, SpaAccount
from app.services import appointment_booking_service as booking
from app.schemas import AppointmentCreate, AppointmentRead, AppointmentUpdate, Page
from app.services.appointment_booking_service import StaffChangeRefused, apply_staff_change

ALREADY_CANCELLED = "This appointment is already cancelled."

router = APIRouter(prefix="/appointments", tags=["appointments"])


from pydantic import BaseModel, EmailStr
from app.api.deps import get_tenant_manager


class CardEmailRequest(BaseModel):
    confirmed_email: EmailStr
    consent: bool
    request_id: uuid.UUID


@router.post("/{appointment_id}/card-email", dependencies=[Depends(get_tenant_manager)])
async def email_card_link(
    appointment_id: uuid.UUID,
    payload: CardEmailRequest,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
):
    """Owner-authorized transactional email; never creates or cancels a booking."""
    from app.models.spa_account import SpaAccount
    from app.models.external_customer_link import ExternalCustomerLink
    from app.models.card_entry_token import CardEntryToken
    from app.services.card_entry import SqlCardEntryStore
    from app.services.card_email import offer_secure_card_email
    from app.services.booking_adapters.spa_router import SpaBookingAdapter

    appointment = (await db.execute(select(Appointment).where(
        Appointment.id == appointment_id, scope_filter(scope, Appointment)
    ).with_for_update())).scalar_one_or_none()
    if appointment is None or scope.tenant_id is None:
        raise HTTPException(404, "Appointment not found")
    existing = (await db.execute(select(CardEntryToken).where(
        CardEntryToken.appointment_id == appointment.id,
        CardEntryToken.idempotency_key == f"email:{payload.request_id}",
    ))).scalar_one_or_none()
    if existing is not None:
        return {"status": "already_attempted", "delivered": False}
    spa = await db.get(SpaAccount, scope.tenant_id)
    if spa is None:
        raise HTTPException(404, "Spa not found")
    contact = await db.get(Contact, appointment.contact_id)
    link = (await db.execute(select(ExternalCustomerLink).where(
        ExternalCustomerLink.tenant_id == scope.tenant_id,
        ExternalCustomerLink.contact_id == appointment.contact_id,
        ExternalCustomerLink.provider == appointment.booking_provider,
    ))).scalar_one_or_none()
    result = await offer_secure_card_email(
        store=SqlCardEntryStore(db), spa=spa, appointment=appointment,
        contact=contact, link=link, adapter=SpaBookingAdapter(spa),
        confirmed_email=str(payload.confirmed_email), consent=payload.consent,
        request_id=payload.request_id,
    )
    return {"status": result, "delivered": False}


@router.post("", response_model=AppointmentRead, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    payload: AppointmentCreate,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Appointment:
    contact = await db.get(Contact, payload.contact_id)
    if not contact or not owns(scope, contact):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")

    await booking._lock_slot(db, scope, payload.start_time, payload.end_time)
    spa = await db.get(SpaAccount, scope.tenant_id) if scope.tenant_id else None
    if await booking._has_conflict(
        db, scope, payload.start_time, payload.end_time, booking._capacity(spa)
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "Time slot conflicts with an existing appointment")

    appointment = Appointment(
        **payload.model_dump(), **scope_columns(scope, Appointment)
    )
    db.add(appointment)
    await db.commit()
    await db.refresh(appointment)
    return appointment


@router.get("", response_model=Page[AppointmentRead])
async def list_appointments(
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=100),
    status_filter: AppointmentStatus | None = Query(None, alias="status"),
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Page[AppointmentRead]:
    try:
        from_time = utc_instant(from_time) if from_time else None
        to_time = utc_instant(to_time) if to_time else None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    query = select(Appointment).where(scope_filter(scope, Appointment))
    if status_filter:
        query = query.where(Appointment.status == status_filter)
    if from_time:
        query = query.where(Appointment.start_time >= from_time)
    if to_time:
        query = query.where(Appointment.start_time <= to_time)

    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            query.order_by(Appointment.start_time.asc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    from app.models.spa_account import SpaAccount
    from app.core.config import settings
    spa = await db.get(SpaAccount, scope.tenant_id) if scope.tenant_id else None
    zone = spa.timezone if spa else settings.SALES_TIMEZONE
    return Page(
        items=[AppointmentRead.model_validate(r).model_copy(update={"business_timezone": zone}) for r in rows],
        total=total, page=page, size=size,
    )


@router.get("/{appointment_id}", response_model=AppointmentRead)
async def get_appointment(
    appointment_id: uuid.UUID,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Appointment:
    appointment = await db.get(Appointment, appointment_id)
    if not appointment or not owns(scope, appointment):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    return appointment


@router.patch("/{appointment_id}", response_model=AppointmentRead, dependencies=[Depends(get_tenant_manager)])
async def update_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentUpdate,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Appointment:
    appointment = await db.get(Appointment, appointment_id)
    if not appointment or not owns(scope, appointment):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    # Share the voice calendar lock through the local commit. Reload after
    # waiting so decisions use the current row, not a pre-lock snapshot.
    await booking._lock_slot(db, scope, appointment.start_time, appointment.end_time)
    await db.refresh(appointment)
    changes = payload.model_dump(exclude_unset=True)
    if (changes.get("status") == AppointmentStatus.CANCELLED
            and appointment.status == AppointmentStatus.CANCELLED):
        # Decided on the row as it is AFTER the lock: a racing or repeated
        # cancel is told someone already cancelled it, and nothing is written.
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_CANCELLED)
    # SQLite returns UTC storage values without tzinfo; API payloads already
    # require an explicit offset. PostgreSQL rows remain timezone-aware.
    old_start = appointment.start_time.replace(tzinfo=timezone.utc) if appointment.start_time.tzinfo is None else appointment.start_time
    old_end = appointment.end_time.replace(tzinfo=timezone.utc) if appointment.end_time.tzinfo is None else appointment.end_time
    new_start = changes.get("start_time", old_start)
    new_end = changes.get("end_time", old_end)
    active = {AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED}
    if appointment.status not in active and (
        (new_start, new_end) != (old_start, old_end) or changes.get("status") in active
    ):
        raise HTTPException(status.HTTP_409_CONFLICT, "An inactive appointment cannot be moved or reactivated")
    if new_end <= new_start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end_time must be after start_time")
    # A booking mirrored in an external calendar changes there first; the
    # local row follows only once the calendar has verifiably accepted it.
    cancelling = (
        changes.get("status") == AppointmentStatus.CANCELLED
        and appointment.status != AppointmentStatus.CANCELLED
    )
    moving = (new_start, new_end) != (old_start, old_end)
    if moving and not cancelling:
        spa = await db.get(SpaAccount, scope.tenant_id) if scope.tenant_id else None
        if await booking._has_conflict(
            db, scope, new_start, new_end, booking._capacity(spa), exclude_id=appointment.id
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "Time slot conflicts with an existing appointment")
    if cancelling or moving:
        try:
            await apply_staff_change(
                db, scope, appointment, start=new_start, end=new_end, cancel=cancelling
            )
        except StaffChangeRefused as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
    for field, value in changes.items():
        setattr(appointment, field, value)
    await db.commit()
    await db.refresh(appointment)
    return appointment


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(get_tenant_manager)])
async def cancel_appointment(
    appointment_id: uuid.UUID,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> None:
    appointment = await db.get(Appointment, appointment_id)
    if not appointment or not owns(scope, appointment):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    await booking._lock_slot(db, scope, appointment.start_time, appointment.end_time)
    await db.refresh(appointment)
    if appointment.status == AppointmentStatus.CANCELLED:
        raise HTTPException(status.HTTP_409_CONFLICT, ALREADY_CANCELLED)
    if appointment.status not in {AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED}:
        raise HTTPException(status.HTTP_409_CONFLICT, "An inactive appointment cannot be cancelled")
    try:
        await apply_staff_change(db, scope, appointment, cancel=True)
    except StaffChangeRefused as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    appointment.status = AppointmentStatus.CANCELLED
    await db.commit()
