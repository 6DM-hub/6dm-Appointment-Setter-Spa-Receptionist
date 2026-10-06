"""6DM Sales Agent — outbound B2B lead pipeline.

Every route here is `super_admin`-only. A spa_admin or spa_staff token gets 403,
which is the contract the dashboard relies on when it hides the entire Sales
Agent section: the navigation is a convenience, this is the enforcement.

Leads are `Contact` rows in the sales workspace (`tenant_id IS NULL`), so they
share storage with the rest of the CRM but can never appear in a spa's guest
list — the scope filter excludes them by construction.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_super_admin
from app.core.tenancy import TenantScope, owns, scope_columns, scope_filter
from app.models import Appointment, AppointmentStatus, CallLog, Contact, User
from app.schemas import Page
from app.schemas.lead import LeadCreate, LeadRead, LeadUpdate

router = APIRouter(
    prefix="/leads",
    tags=["6dm-sales"],
    dependencies=[Depends(get_super_admin)],
)


def _sales_scope(user: User) -> TenantScope:
    return TenantScope.for_sales_workspace(user.id)


def _pipeline_query(scope: TenantScope) -> Select:
    """Contacts in the sales workspace, decorated with call/appointment
    aggregates via correlated subqueries so one round trip fills the table."""
    call_count = (
        select(func.count(CallLog.id))
        .where(CallLog.contact_id == Contact.id)
        .correlate(Contact)
        .scalar_subquery()
    )
    last_call_at = (
        select(func.max(CallLog.created_at))
        .where(CallLog.contact_id == Contact.id)
        .correlate(Contact)
        .scalar_subquery()
    )
    upcoming = (
        select(func.min(Appointment.start_time))
        .where(
            Appointment.contact_id == Contact.id,
            Appointment.start_time >= datetime.now(timezone.utc),
            Appointment.status.in_(
                [AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]
            ),
        )
        .correlate(Contact)
        .scalar_subquery()
    )
    return select(
        Contact,
        call_count.label("call_count"),
        last_call_at.label("last_call_at"),
        upcoming.label("upcoming_appointment_at"),
    ).where(scope_filter(scope, Contact))


def _to_lead(row) -> LeadRead:
    lead = LeadRead.model_validate(row.Contact)
    return lead.model_copy(
        update={
            "call_count": row.call_count or 0,
            "last_call_at": row.last_call_at,
            "upcoming_appointment_at": row.upcoming_appointment_at,
        }
    )


@router.get("", response_model=Page[LeadRead])
async def list_leads(
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=100),
    search: str | None = None,
    booked: bool | None = Query(
        None, description="Filter to leads with / without an upcoming presentation"
    ),
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
) -> Page[LeadRead]:
    scope = _sales_scope(current_user)
    conditions = [scope_filter(scope, Contact)]

    if search:
        pattern = f"%{search}%"
        conditions.append(
            Contact.first_name.ilike(pattern)
            | Contact.last_name.ilike(pattern)
            | Contact.phone_number.ilike(pattern)
            | Contact.email.ilike(pattern)
        )

    if booked is not None:
        exists_upcoming = (
            select(Appointment.id)
            .where(
                Appointment.contact_id == Contact.id,
                Appointment.start_time >= datetime.now(timezone.utc),
                Appointment.status.in_(
                    [AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED]
                ),
            )
            .correlate(Contact)
            .exists()
        )
        conditions.append(exists_upcoming if booked else ~exists_upcoming)

    # Counted off the bare table: running the per-lead aggregates just to
    # discard them would triple the work for every page render.
    total = (
        await db.execute(
            select(func.count()).select_from(Contact).where(*conditions)
        )
    ).scalar_one()
    rows = (
        await db.execute(
            _pipeline_query(scope)
            .where(*conditions[1:])
            .order_by(Contact.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).all()
    return Page(
        items=[_to_lead(row) for row in rows], total=total, page=page, size=size
    )


@router.post("", response_model=LeadRead, status_code=status.HTTP_201_CREATED)
async def create_lead(
    payload: LeadCreate,
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
) -> LeadRead:
    scope = _sales_scope(current_user)
    existing = (
        await db.execute(
            select(Contact).where(
                scope_filter(scope, Contact),
                Contact.phone_number == payload.phone_number,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Lead with this phone exists")

    lead = Contact(**payload.model_dump(), **scope_columns(scope, Contact))
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return LeadRead.model_validate(lead)


async def _get_owned_lead(
    db: AsyncSession, scope: TenantScope, lead_id: uuid.UUID
) -> Contact:
    lead = await db.get(Contact, lead_id)
    if not lead or not owns(scope, lead):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
    return lead


@router.get("/{lead_id}", response_model=LeadRead)
async def get_lead(
    lead_id: uuid.UUID,
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
) -> LeadRead:
    scope = _sales_scope(current_user)
    await _get_owned_lead(db, scope, lead_id)
    row = (
        await db.execute(_pipeline_query(scope).where(Contact.id == lead_id))
    ).one()
    return _to_lead(row)


@router.patch("/{lead_id}", response_model=LeadRead)
async def update_lead(
    lead_id: uuid.UUID,
    payload: LeadUpdate,
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
) -> LeadRead:
    scope = _sales_scope(current_user)
    lead = await _get_owned_lead(db, scope, lead_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(lead, field, value)
    await db.commit()
    await db.refresh(lead)
    return LeadRead.model_validate(lead)


@router.delete("/{lead_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_lead(
    lead_id: uuid.UUID,
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    scope = _sales_scope(current_user)
    lead = await _get_owned_lead(db, scope, lead_id)
    await db.delete(lead)
    await db.commit()
