import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ServiceCategoryCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = Field(None, max_length=1000)


class ServiceCategoryUpdate(ServiceCategoryCreate):
    is_active: bool | None = None


class ServiceCategoryRead(ORMModel):
    id: uuid.UUID
    name: str
    description: str | None
    is_active: bool
    service_count: int = 0
    created_at: datetime
    updated_at: datetime


class ServiceCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    category_id: uuid.UUID
    description: str | None = Field(None, max_length=1000)
    price: str | None = Field(None, max_length=32)
    duration_minutes: int = Field(60, ge=5, le=600)
    is_active: bool = True


class ServiceUpdate(ServiceCreate):
    pass


class ServiceRead(ORMModel):
    id: uuid.UUID
    category_id: uuid.UUID
    category_name: str = ""
    name: str
    description: str | None
    price: str | None
    duration_minutes: int
    is_active: bool
    square_item_id: str | None = None
    square_variation_id: str | None = None
    square_variation_version: int | None = None
    created_at: datetime
    updated_at: datetime


class SquareCatalogSelection(BaseModel):
    square_variation_id: str = Field(..., min_length=1, max_length=255)
    action: Literal["add", "update", "skip"]
    category_id: uuid.UUID | None = None


class SquareCatalogApply(BaseModel):
    preview_token: str = Field(..., min_length=20, max_length=4096)
    # fetch_square_catalog reads at most 25 pages of 100 items. Keep apply's
    # approval capacity aligned with that bounded provider snapshot.
    selections: list[SquareCatalogSelection] = Field(default_factory=list, max_length=2500)


class SquareCatalogFieldChange(BaseModel):
    field: str
    current: str | int | bool | None = None
    square: str | int | bool | None = None


class SquareCatalogItem(BaseModel):
    square_item_id: str
    square_variation_id: str
    square_version: int | None = None
    name: str
    category_name: str | None = None
    duration_minutes: int
    price: str | None = None
    action: Literal["add", "update", "conflict", "unchanged"]
    existing_service_id: uuid.UUID | None = None
    changes: list[SquareCatalogFieldChange] = Field(default_factory=list)
    conflict_reason: str | None = None


class SquareCatalogSummary(BaseModel):
    additions: int = 0
    updates: int = 0
    conflicts: int = 0
    unchanged: int = 0


class SquareCatalogPreview(BaseModel):
    preview_token: str | None = None
    connected: bool
    location_name: str | None = None
    generated_at: datetime
    summary: SquareCatalogSummary
    items: list[SquareCatalogItem]


class SquareCatalogApplyResult(BaseModel):
    created: int
    updated: int
    skipped: int
    conflicts: int
