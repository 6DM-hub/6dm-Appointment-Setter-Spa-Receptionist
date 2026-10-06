import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_tenant_scope
from app.core.tenancy import TenantScope, owns, scope_columns, scope_filter
from app.models import Appointment, AppointmentStatus, Contact
from app.schemas import AppointmentCreate, AppointmentRead, AppointmentUpdate, Page

router = APIRouter(prefix="/appointments", tags=["appointments"])


@router.post("", response_model=AppointmentRead, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    payload: AppointmentCreate,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Appointment:
    contact = await db.get(Contact, payload.contact_id)
    if not contact or not owns(scope, contact):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")

    overlap = (
        await db.execute(
            select(Appointment).where(
                scope_filter(scope, Appointment),
                Appointment.status.in_(
                    [AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]
                ),
                Appointment.start_time < payload.end_time,
                Appointment.end_time > payload.start_time,
            )
        )
    ).scalar_one_or_none()
    if overlap:
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
    return Page(
        items=[AppointmentRead.model_validate(r) for r in rows],
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


@router.patch("/{appointment_id}", response_model=AppointmentRead)
async def update_appointment(
    appointment_id: uuid.UUID,
    payload: AppointmentUpdate,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Appointment:
    appointment = await db.get(Appointment, appointment_id)
    if not appointment or not owns(scope, appointment):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(appointment, field, value)
    if appointment.end_time <= appointment.start_time:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "end_time must be after start_time")
    await db.commit()
    await db.refresh(appointment)
    return appointment


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_appointment(
    appointment_id: uuid.UUID,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> None:
    appointment = await db.get(Appointment, appointment_id)
    if not appointment or not owns(scope, appointment):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Appointment not found")
    appointment.status = AppointmentStatus.CANCELLED
    await db.commit()
