import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db, get_tenant_manager, get_tenant_scope
from app.core.tenancy import TenantScope
from app.models import BookingProvider, Service, ServiceCategory, SpaAccount
from app.schemas import (
    ServiceCategoryCreate,
    ServiceCategoryRead,
    ServiceCategoryUpdate,
    ServiceCreate,
    ServiceRead,
    ServiceUpdate,
    SquareCatalogApply,
    SquareCatalogApplyResult,
    SquareCatalogPreview,
)
from app.services.booking_adapters import BookingProviderError
from app.services.booking_config import decrypt_config, missing_config
from app.services.square_catalog_sync import (
    StaleCatalogPreview,
    build_preview,
    catalog_fingerprint,
    fetch_square_catalog,
    generated_at,
    local_fingerprint,
    make_preview_token,
    provider_identity_fingerprint,
    verify_preview_token,
)

router = APIRouter(prefix="/services", tags=["services"])

_LEGACY_NORMALIZED_KEYS = frozenset(
    {
        "id",
        "name",
        "duration_minutes",
        "price",
        "description",
        "category",
        "category_id",
        "is_active",
        "square_item_id",
        "square_variation_id",
        "square_variation_version",
    }
)


class ServiceBulkDelete(BaseModel):
    service_ids: list[uuid.UUID] | None = None
    delete_all: bool = False


def _spa_id(scope: TenantScope) -> uuid.UUID:
    if scope.tenant_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Select a spa tenant first")
    return scope.tenant_id


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
        square_item_id=service.square_item_id,
        square_variation_id=service.square_variation_id,
        square_variation_version=service.square_variation_version,
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


def _legacy_service_name(value: object) -> str:
    normalized = re.sub(
        r"\b\d+\s*(?:minute|min|minutes|hour|hours|hr|hrs)\b",
        " ",
        str(value or "").casefold(),
    )
    return " ".join(re.sub(r"[^a-z0-9]+", " ", normalized).split())


def _find_legacy_service(entries: list[dict], service: Service) -> dict | None:
    if service.square_variation_id:
        matches = [
            item
            for item in entries
            if str(item.get("square_variation_id") or "") == service.square_variation_id
        ]
        if len(matches) == 1:
            return matches[0]
    exact = [
        item
        for item in entries
        if _legacy_service_name(item.get("name")) == _legacy_service_name(service.name)
        and item.get("duration_minutes") == service.duration_minutes
    ]
    if len(exact) == 1:
        return exact[0]
    # This fallback is used only to capture metadata before an approved Square
    # update changes a duration. Ambiguous 30/60 rows are deliberately ignored.
    family = [
        item
        for item in entries
        if _legacy_service_name(item.get("name")) == _legacy_service_name(service.name)
    ]
    return family[0] if len(family) == 1 else None


async def _sync_legacy_services(
    db: AsyncSession,
    spa_id: uuid.UUID,
    *,
    legacy_overrides: dict[uuid.UUID, dict] | None = None,
) -> None:
    spa = await db.get(SpaAccount, spa_id)
    if spa is None:
        return
    rows = (
        await db.execute(
            select(Service, ServiceCategory.name)
            .join(ServiceCategory, Service.category_id == ServiceCategory.id)
            .where(Service.spa_id == spa_id)
            .order_by(Service.created_at.asc())
        )
    ).all()
    # Keep owner-authored consultation, aliases, buffers, and upsell metadata
    # while refreshing normalized fields. Previously this rewrite discarded
    # Square ids and could silently send voice booking back to text matching.
    existing = [item for item in (spa.services or []) if isinstance(item, dict)]
    by_variation = {
        str(item.get("square_variation_id")): item
        for item in existing
        if item.get("square_variation_id")
    }
    by_name_duration = {
        (_legacy_service_name(item.get("name")), item.get("duration_minutes")): item
        for item in existing
    }
    synchronized: list[dict] = []
    for service, category_name in rows:
        prior = (legacy_overrides or {}).get(service.id)
        if prior is None:
            prior = by_variation.get(str(service.square_variation_id or ""))
        if prior is None:
            prior = by_name_duration.get((_legacy_service_name(service.name), service.duration_minutes))
        metadata = dict(service.voice_metadata or {})
        if prior is not None:
            # The active legacy menu is still where the voice-only editor
            # writes today. Capture exactly its non-normalized fields before
            # an item is deactivated or renamed, then retain them while the
            # item is absent from that active-only JSON list.
            metadata = {
                key: value
                for key, value in prior.items()
                if key not in _LEGACY_NORMALIZED_KEYS
            }
        service.voice_metadata = metadata
        if not service.is_active:
            continue
        item = dict(metadata)
        item.update({
            "name": service.name,
            "duration_minutes": service.duration_minutes,
            "price": service.price,
            "description": service.description,
            "category": category_name,
            "square_item_id": service.square_item_id,
            "square_variation_id": service.square_variation_id,
            "square_variation_version": service.square_variation_version,
        })
        synchronized.append(item)
    spa.services = synchronized


@router.get("/categories", response_model=list[ServiceCategoryRead])
async def list_categories(
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> list[ServiceCategoryRead]:
    spa_id = _spa_id(scope)
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
    return [
        ServiceCategoryRead.model_validate(category).model_copy(
            update={"service_count": count}
        )
        for category, count in rows
    ]


@router.post(
    "/categories",
    response_model=ServiceCategoryRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_tenant_manager)],
)
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


@router.patch(
    "/categories/{category_id}",
    response_model=ServiceCategoryRead,
    dependencies=[Depends(get_tenant_manager)],
)
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


@router.delete(
    "/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_tenant_manager)],
)
async def delete_category(
    category_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> None:
    spa_id = _spa_id(scope)
    category = (
        await db.execute(
            select(ServiceCategory)
            .where(
                ServiceCategory.id == category_id,
                ServiceCategory.spa_id == spa_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if category is None:
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
    query = select(Service).options(selectinload(Service.category)).where(Service.spa_id == spa_id).order_by(Service.name.asc())
    if category_id:
        query = query.where(Service.category_id == category_id)
    rows = (await db.execute(query)).scalars().all()
    return [_service_read(service) for service in rows]


async def _catalog_state(
    db: AsyncSession,
    spa_id: uuid.UUID,
) -> tuple[SpaAccount, list[Service], list[dict] | None, str | None, str | None]:
    """Return tenant state plus a live Square snapshot, or None if disconnected."""
    spa = await db.get(SpaAccount, spa_id)
    if spa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")
    services = list(
        (
            await db.execute(
                select(Service).where(Service.spa_id == spa_id).order_by(Service.id.asc())
            )
        ).scalars()
    )
    if spa.booking_provider is not BookingProvider.SQUARE:
        return spa, services, None, None, None
    config = decrypt_config(spa.booking_config)
    if missing_config(BookingProvider.SQUARE, spa.booking_config):
        return spa, services, None, None, None
    provider_hash = provider_identity_fingerprint(config)
    try:
        catalog, location_name = await fetch_square_catalog(spa, config)
    except BookingProviderError as exc:
        # Provider details can contain internal ids. Give the owner an
        # actionable category without reflecting raw Square errors or tokens.
        detail = "Square catalog access failed. Reconnect Square or verify Catalog read permission."
        if exc.retryable:
            detail = "Square is temporarily unavailable. Refresh the catalog preview shortly."
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail) from exc
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "Square returned an invalid catalog response. Refresh and try again.",
        ) from exc
    return spa, services, catalog, location_name, provider_hash


async def _lock_catalog_apply_state(
    db: AsyncSession,
    spa_id: uuid.UUID,
) -> tuple[SpaAccount, list[Service], str]:
    """Re-read and lock all local state covered by a catalog approval.

    The live Square fetch deliberately happens before these locks so a slow
    provider request cannot block dashboard writes. Once acquired, the locks
    are held through the catalog mutation and commit. ``populate_existing``
    prevents SQLAlchemy's identity map from returning values read before the
    provider round trip.
    """
    spa_result = await db.execute(
        select(SpaAccount)
        .where(SpaAccount.id == spa_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    spa = spa_result.scalar_one_or_none()
    if spa is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Spa account not found")

    service_result = await db.execute(
        select(Service)
        .where(Service.spa_id == spa_id)
        .order_by(Service.id.asc())
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    services = list(service_result.scalars())

    if spa.booking_provider is not BookingProvider.SQUARE or missing_config(
        BookingProvider.SQUARE, spa.booking_config
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Square connection changed; refresh the preview",
        )
    config = decrypt_config(spa.booking_config)
    return spa, services, provider_identity_fingerprint(config)


@router.get(
    "/square-catalog/preview",
    response_model=SquareCatalogPreview,
    dependencies=[Depends(get_tenant_manager)],
)
async def preview_square_catalog(
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> SquareCatalogPreview:
    """Read-only, owner-visible diff of live Square services versus Cara."""
    spa_id = _spa_id(scope)
    _, services, catalog, location_name, provider_hash = await _catalog_state(db, spa_id)
    if catalog is None:
        return SquareCatalogPreview(
            preview_token=None,
            connected=False,
            location_name=None,
            generated_at=generated_at(),
            summary={"additions": 0, "updates": 0, "conflicts": 0, "unchanged": 0},
            items=[],
        )
    items, summary = build_preview(catalog, services)
    token = make_preview_token(
        spa_id,
        catalog_fingerprint(catalog),
        local_fingerprint(services),
        provider_hash or "",
    )
    return SquareCatalogPreview(
        preview_token=token,
        connected=True,
        location_name=location_name,
        generated_at=generated_at(),
        summary=summary,
        items=items,
    )


@router.post(
    "/square-catalog/apply",
    response_model=SquareCatalogApplyResult,
    dependencies=[Depends(get_tenant_manager)],
)
async def apply_square_catalog(
    payload: SquareCatalogApply,
    db: AsyncSession = Depends(get_db),
    scope: TenantScope = Depends(get_tenant_scope),
) -> SquareCatalogApplyResult:
    """Apply only explicitly selected preview rows after a live stale check."""
    spa_id = _spa_id(scope)
    _, _, catalog, _, fetched_provider_hash = await _catalog_state(db, spa_id)
    if catalog is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Connect Square before importing services")
    spa, services, provider_hash = await _lock_catalog_apply_state(db, spa_id)
    if fetched_provider_hash != provider_hash:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Square connection changed; refresh the preview",
        )
    try:
        verify_preview_token(
            payload.preview_token,
            spa_id,
            catalog_fingerprint(catalog),
            local_fingerprint(services),
            provider_hash,
        )
    except StaleCatalogPreview as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    selection_ids = [selection.square_variation_id for selection in payload.selections]
    if len(selection_ids) != len(set(selection_ids)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Select each Square variation only once")

    preview, _ = build_preview(catalog, services)
    catalog_by_id = {item["square_variation_id"]: item for item in preview}
    service_by_id = {str(service.id): service for service in services}
    if any(
        selection.action == "update" and selection.category_id is not None
        for selection in payload.selections
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Category changes are not part of Square updates; edit the Cara category separately",
        )
    requested_category_ids = {
        selection.category_id
        for selection in payload.selections
        if selection.action == "add" and selection.category_id is not None
    }
    categories = {}
    if requested_category_ids:
        categories = {
            category.id: category
            for category in (
                await db.execute(
                    select(ServiceCategory)
                    .where(
                        ServiceCategory.spa_id == spa_id,
                        ServiceCategory.id.in_(requested_category_ids),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
    created = updated = skipped = conflicts = 0
    legacy_entries = [item for item in (spa.services or []) if isinstance(item, dict)]
    legacy_overrides: dict[uuid.UUID, dict] = {}

    for selection in payload.selections:
        item = catalog_by_id.get(selection.square_variation_id)
        if item is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A selected Square service is no longer present; refresh the preview",
            )
        if selection.action == "skip":
            skipped += 1
            if item["action"] == "conflict":
                conflicts += 1
            continue
        if item["action"] == "conflict":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Resolve the local conflict for {item['name']} before importing it",
            )
        category = None
        if selection.category_id is not None:
            category = categories.get(selection.category_id)
            if category is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Choose a category belonging to this spa",
                )

        if selection.action == "add":
            if item["action"] != "add":
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{item['name']} already matches a Cara service; refresh the preview",
                )
            if category is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Choose a category before adding {item['name']}",
                )
            service = Service(
                spa_id=spa_id,
                category_id=category.id,
                name=item["name"],
                description=item["description"],
                price=item["price"],
                duration_minutes=item["duration_minutes"],
                is_active=True,
                square_item_id=item["square_item_id"],
                square_variation_id=item["square_variation_id"],
                square_variation_version=item["square_version"],
            )
            db.add(service)
            created += 1
        elif selection.action == "update":
            if item["action"] not in {"update", "unchanged"}:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{item['name']} has no unambiguous Cara service to update",
                )
            service = service_by_id.get(str(item["existing_service_id"] or ""))
            if service is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "Matched Cara service changed; refresh the preview")
            prior_legacy = _find_legacy_service(legacy_entries, service)
            if prior_legacy is not None:
                legacy_overrides[service.id] = dict(prior_legacy)
            service.name = item["name"]
            service.duration_minutes = item["duration_minutes"]
            service.price = item["price"]
            # Keep owner-authored descriptions. Square fills only an empty one.
            if not service.description and item["description"]:
                service.description = item["description"]
            service.square_item_id = item["square_item_id"]
            service.square_variation_id = item["square_variation_id"]
            service.square_variation_version = item["square_version"]
            updated += 1

    if created == 0 and updated == 0:
        return SquareCatalogApplyResult(
            created=0,
            updated=0,
            skipped=skipped,
            conflicts=conflicts,
        )

    try:
        await db.flush()
        await _sync_legacy_services(db, spa_id, legacy_overrides=legacy_overrides)
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        if code == "23503":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "A selected category changed or was deleted; refresh the preview",
            ) from exc
        if code != "23505":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Square catalog changes could not be saved; refresh the preview",
            ) from exc
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A Square variation is already linked to another Cara service; refresh the preview",
        ) from exc

    return SquareCatalogApplyResult(
        created=created,
        updated=updated,
        skipped=skipped,
        conflicts=conflicts,
    )


@router.post("/bulk-delete", dependencies=[Depends(get_tenant_manager)])
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


@router.post(
    "",
    response_model=ServiceRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_tenant_manager)],
)
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


@router.patch(
    "/{service_id}",
    response_model=ServiceRead,
    dependencies=[Depends(get_tenant_manager)],
)
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


@router.delete(
    "/{service_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(get_tenant_manager)],
)
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
