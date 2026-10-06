import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.api.deps import get_db, get_tenant_scope
from app.core.tenancy import TenantScope, owns, scope_columns, scope_filter
from app.models import Contact
from app.schemas import ContactCreate, ContactRead, ContactUpdate, Page

router = APIRouter(prefix="/contacts", tags=["contacts"])


@router.post("", response_model=ContactRead, status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Contact:
    existing = (
        await db.execute(
            select(Contact).where(
                scope_filter(scope, Contact),
                Contact.phone_number == payload.phone_number,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Contact with this phone exists")

    contact = Contact(**payload.model_dump(), **scope_columns(scope, Contact))
    db.add(contact)
    await db.commit()
    await db.refresh(contact)
    return contact


@router.get("", response_model=Page[ContactRead])
async def list_contacts(
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=100),
    search: str | None = None,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Page[ContactRead]:
    query = (
        select(Contact)
        .options(noload(Contact.appointments), noload(Contact.call_logs))
        .where(scope_filter(scope, Contact))
    )
    if search:
        pattern = f"%{search}%"
        query = query.where(
            Contact.first_name.ilike(pattern)
            | Contact.last_name.ilike(pattern)
            | Contact.phone_number.ilike(pattern)
        )
    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            query.order_by(Contact.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return Page(
        items=[ContactRead.model_validate(r) for r in rows],
        total=total, page=page, size=size,
    )


@router.get("/{contact_id}", response_model=ContactRead)
async def get_contact(
    contact_id: uuid.UUID,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Contact:
    contact = await db.get(Contact, contact_id)
    # 404 rather than 403 on a cross-tenant id: whether some other spa's guest
    # exists is not this caller's business.
    if not contact or not owns(scope, contact):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    return contact


@router.patch("/{contact_id}", response_model=ContactRead)
async def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdate,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Contact:
    contact = await db.get(Contact, contact_id)
    if not contact or not owns(scope, contact):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(contact, field, value)
    await db.commit()
    await db.refresh(contact)
    return contact


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: uuid.UUID,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> None:
    contact = await db.get(Contact, contact_id)
    if not contact or not owns(scope, contact):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contact not found")
    await db.delete(contact)
    await db.commit()
