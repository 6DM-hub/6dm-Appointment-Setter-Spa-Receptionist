import uuid
from datetime import datetime

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
    created_at: datetime
    updated_at: datetime