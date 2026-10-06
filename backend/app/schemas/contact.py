import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMModel


class ContactBase(BaseModel):
    first_name: str | None = Field(None, max_length=120)
    last_name: str | None = Field(None, max_length=120)
    phone_number: str = Field(..., min_length=7, max_length=32)
    email: EmailStr | None = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        return value or None

    @field_validator("phone_number")
    @classmethod
    def normalize_phone(cls, v: str) -> str:
        cleaned = v.strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
        if not cleaned.startswith("+"):
            raise ValueError("phone_number must be in E.164 format (e.g. +15551234567)")
        return cleaned


class ContactCreate(ContactBase):
    pass


class ContactUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone_number: str | None = None
    email: EmailStr | None = None
    extra_metadata: dict[str, Any] | None = None


class ContactRead(ORMModel):
    """Dashboard/API view of a stored contact.

    Intentionally does not reuse ContactBase validators: a single receptionist
    row with a non-E.164 phone or a spoken non-email would otherwise make
    GET /contacts 500 and the spa dashboard show "Unable to load guests."
    """

    id: uuid.UUID
    owner_id: uuid.UUID | None
    tenant_id: uuid.UUID | None
    first_name: str | None = None
    last_name: str | None = None
    phone_number: str
    email: str | None = None
    extra_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    full_name: str = "Unknown"

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = str(v).strip()
        return value or None

    @field_validator("extra_metadata", mode="before")
    @classmethod
    def normalize_metadata(cls, v: dict[str, Any] | None) -> dict[str, Any]:
        return v or {}