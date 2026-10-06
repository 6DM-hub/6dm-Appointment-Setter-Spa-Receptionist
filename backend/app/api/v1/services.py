import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db, get_tenant_scope
from app.core.tenancy import TenantScope
from app.models import Service, ServiceCategory, SpaAccount
from app.schemas import (
    ServiceCategoryCreate,
    ServiceCategoryRead,
    ServiceCategoryUpdate,
    ServiceCreate,
    ServiceRead,
    ServiceUpdate,
)

router = APIRouter(prefix="/services", tags=["services"])

DEFAULT_CATEGORIES = (
    "Facial",
    "Massage",
    "MedSpa",
    "Wellness Treatment",
    "Waxing",
    "Spa Packages",
)


class ServiceBulkDelete(BaseModel):
    service_ids: list[uuid.UUID] | None = None
    delete_all: bool = False


def _spa_id(scope: TenantScope) -> uuid.UUID:
    if scope.tenant_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Select a spa tenant first")
    return scope.tenant_id


async def _ensure_defaults(db: AsyncSession, spa_id: uuid.UUID) -> None:
    existing = set(
        (
            await db.execute(
                select(ServiceCategory.name).where(ServiceCategory.spa_id == spa_id)
            )
        ).scalars()
    )
    missing = [name for name in DEFAULT_CATEGORIES if name not in existing]
    if missing:
        db.add_all([ServiceCategory(spa_id=spa_id, name=name) for name in missing])
        await db.flush()


def _service_read(service: Service) -> ServiceRead:
    return ServiceRead(
        id=service.id,
        category_id=service.category_id,
        category_name=service.category.name,
        name=service.name,
        description=service.description,
        price=service.price,
        duration_minutes=service.duration_minutes,
        is_active=service.is_active,
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


async def _sync_legacy_services(db: AsyncSession, spa_id: uuid.UUID) -> None:
    spa = await db.get(SpaAccount, spa_id)
    if spa is None:
        return
    rows = (
        await db.execute(
            select(Service, ServiceCategory.name)
            .join(ServiceCategory, Service.category_id == ServiceCategory.id)
            .where(Service.spa_id == spa_id, Service.is_active.is_(True))
            .order_by(Service.created_at.asc())
        )
    ).all()
    spa.services = [
        {
            "name": service.name,
            "duration_minutes": service.duration_minutes,
            "price": service.price,
            "description": service.description,
            "category": category_name,
        }
        for service, category_name in rows
    ]


@router.get("/categories", response_model=list[ServiceCategoryRead])
async def list_categories(
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> list[ServiceCategoryRead]:
    spa_id = _spa_id(scope)
    await _ensure_defaults(db, spa_id)
    rows = (
        await db.execute(
            select(
                ServiceCategory,
                func.count(Service.id).label("service_count"),
            )
            .outerjoin(Service, Service.category_id == ServiceCategory.id)
            .where(ServiceCategory.spa_id == spa_id)
            .group_by(ServiceCategory.id)
            .order_by(ServiceCategory.name.asc())
        )
    ).all()
    await db.commit()
    return [
        ServiceCategoryRead.model_validate(category).model_copy(
            update={"service_count": count}
        )
        for category, count in rows
    ]


@router.post("/categories", response_model=ServiceCategoryRead, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: ServiceCategoryCreate,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> ServiceCategoryRead:
    category = ServiceCategory(spa_id=_spa_id(scope), **payload.model_dump())
    db.add(category)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "A category with that name already exists")
    await db.refresh(category)
    return ServiceCategoryRead.model_validate(category)


@router.patch("/categories/{category_id}", response_model=ServiceCategoryRead)
async def update_category(
    category_id: uuid.UUID,
    payload: ServiceCategoryUpdate,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> ServiceCategoryRead:
    category = await db.get(ServiceCategory, category_id)
    if category is None or category.spa_id != _spa_id(scope):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(category, field, value)
    await _sync_legacy_services(db, category.spa_id)
    await db.commit()
    await db.refresh(category)
    return ServiceCategoryRead.model_validate(category)


@router.delete("/categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> None:
    category = await db.get(ServiceCategory, category_id)
    if category is None or category.spa_id != _spa_id(scope):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Category not found")
    count = (
        await db.execute(select(func.count()).select_from(Service).where(Service.category_id == category_id))
    ).scalar_one()
    if count:
        raise HTTPException(status.HTTP_409_CONFLICT, "Move or delete its services before deleting this category")
    await db.delete(category)
    await db.commit()


@router.get("", response_model=list[ServiceRead])
async def list_services(
    category_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> list[ServiceRead]:
    spa_id = _spa_id(scope)
    await _ensure_defaults(db, spa_id)
    query = select(Service).options(selectinload(Service.category)).where(Service.spa_id == spa_id).order_by(Service.name.asc())
    if category_id:
        query = query.where(Service.category_id == category_id)
    rows = (await db.execute(query)).scalars().all()
    await db.commit()
    return [_service_read(service) for service in rows]


@router.post("/bulk-delete")
async def bulk_delete_services(
    payload: ServiceBulkDelete,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> dict[str, int]:
    spa_id = _spa_id(scope)
    if payload.delete_all:
        query = select(Service).where(Service.spa_id == spa_id)
    elif payload.service_ids:
        query = select(Service).where(
            Service.spa_id == spa_id,
            Service.id.in_(payload.service_ids),
        )
    else:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select at least one service")
    services = (await db.execute(query)).scalars().all()
    for service in services:
        await db.delete(service)
    await db.flush()
    await _sync_legacy_services(db, spa_id)
    await db.commit()
    return {"deleted": len(services)}


async def _category_for_service(db: AsyncSession, category_id: uuid.UUID, spa_id: uuid.UUID) -> ServiceCategory:
    category = await db.get(ServiceCategory, category_id)
    if category is None or category.spa_id != spa_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a category belonging to this spa")
    return category


@router.post("", response_model=ServiceRead, status_code=status.HTTP_201_CREATED)
async def create_service(
    payload: ServiceCreate,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> ServiceRead:
    spa_id = _spa_id(scope)
    category = await _category_for_service(db, payload.category_id, spa_id)
    service = Service(spa_id=spa_id, **payload.model_dump())
    db.add(service)
    await db.flush()
    await _sync_legacy_services(db, spa_id)
    await db.commit()
    await db.refresh(service)
    service.category = category
    return _service_read(service)


@router.patch("/{service_id}", response_model=ServiceRead)
async def update_service(
    service_id: uuid.UUID,
    payload: ServiceUpdate,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> ServiceRead:
    spa_id = _spa_id(scope)
    service = await db.get(Service, service_id)
    if service is None or service.spa_id != spa_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    values = payload.model_dump(exclude_unset=True)
    if "category_id" in values:
        await _category_for_service(db, values["category_id"], spa_id)
    for field, value in values.items():
        setattr(service, field, value)
    await _sync_legacy_services(db, spa_id)
    await db.commit()
    await db.refresh(service)
    await db.refresh(service, ["category"])
    return _service_read(service)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> None:
    spa_id = _spa_id(scope)
    service = await db.get(Service, service_id)
    if service is None or service.spa_id != spa_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Service not found")
    await db.delete(service)
    await db.flush()
    await _sync_legacy_services(db, spa_id)
    await db.commit()