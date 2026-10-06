import uuid
from typing import Any

from pydantic import BaseModel, EmailStr, Field, model_validator

from app.models.user import UserRole


class UserRegister(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str | None = Field(None, max_length=255)
    # Only honoured when a super admin is provisioning the account; the
    # bootstrap (first-ever) user is always a super admin.
    role: UserRole = UserRole.SPA_ADMIN
    tenant_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_role_tenant(self) -> "UserRegister":
        if self.role == UserRole.SUPER_ADMIN and self.tenant_id is not None:
            raise ValueError("super_admin accounts must not be assigned a tenant_id")
        if self.role != UserRole.SUPER_ADMIN and self.tenant_id is None:
            raise ValueError(f"{self.role.value} accounts require a tenant_id")
        return self


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserRead(BaseModel):
    id: uuid.UUID
    email: EmailStr
    full_name: str | None
    is_active: bool
    role: UserRole
    tenant_id: uuid.UUID | None
    twilio_phone_number: str | None

    model_config = {"from_attributes": True}


class UserUpdate(BaseModel):
    """Self-service profile edits. Role and tenant are deliberately absent — a
    user must never be able to widen their own scope."""

    full_name: str | None = None
    twilio_phone_number: str | None = None
    workspace_settings: dict[str, Any] | None = None
