import uuid
import re
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from app.api.deps import get_db, get_current_user, get_tenant_manager, get_tenant_scope
from app.models import SpaAccount, User, UserRole
from app.models.booking_escalation import BookingEscalation
from app.services import booking_escalations as service

router = APIRouter(prefix="/booking-escalations", tags=["Needs Staff Attention"], dependencies=[Depends(get_current_user)])


def tenant(scope):
    if not scope.tenant_id:
        raise HTTPException(403, "Select a business.")
    return scope.tenant_id


def serialize(row):
    return {k: getattr(row, k) for k in ("id", "call_reference", "status", "priority", "category", "details", "delivery", "history", "version")}


async def can_resolve(user, scope, db):
    tid = tenant(scope)
    if user.role == UserRole.SUPER_ADMIN or (user.role == UserRole.SPA_ADMIN and user.tenant_id == tid):
        return True
    spa = await db.get(SpaAccount, tid)
    allowed = ((spa.notification_settings or {}).get("booking_escalation") or {}).get("staff_user_ids", [])
    return user.tenant_id == tid and str(user.id) in allowed


async def editor(user=Depends(get_current_user), scope=Depends(get_tenant_scope), db=Depends(get_db)):
    if not await can_resolve(user, scope, db):
        raise HTTPException(403, "Your business administrator must grant staff-request resolution access.")
    return user


@router.get("/access")
async def access(user=Depends(get_current_user), scope=Depends(get_tenant_scope), db=Depends(get_db)):
    return {"can_resolve": await can_resolve(user, scope, db)}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sms_destinations: list[str] = Field(default_factory=list, max_length=10)
    email_destinations: list[str] = Field(default_factory=list, max_length=10)
    notifications_enabled: bool = True
    staff_user_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    categories: list[Literal["permissions", "configuration", "unavailable", "conflict", "temporary", "unknown_outcome"]] = Field(default_factory=list)

    @field_validator("sms_destinations", "email_destinations")
    @classmethod
    def destinations(cls, value, info):
        pattern = r"\+[1-9]\d{7,14}" if info.field_name == "sms_destinations" else r"[^\s@]+@[^\s@]+\.[^\s@]+"
        if any(not re.fullmatch(pattern, v) for v in value):
            raise ValueError("Enter valid email addresses or E.164 phone numbers.")
        return list(dict.fromkeys(value))


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["pending", "contacted", "booked", "unable_to_book"]
    expected_version: int
    note: str = Field(default="", max_length=2000)
    provider_booking_id: str | None = Field(default=None, max_length=255)


class Followup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    consent_note: str = Field(min_length=5, max_length=1000)


@router.post("/{request_id}/customer-followup")
async def followup(request_id: uuid.UUID, body: Followup, scope=Depends(get_tenant_scope),
                   user=Depends(editor), db=Depends(get_db)):
    return await service.customer_followup(db, tenant(scope), request_id, user.id, body.consent_note)


@router.get("")
async def inbox(scope=Depends(get_tenant_scope), db=Depends(get_db)):
    rows = (await db.execute(select(BookingEscalation).where(BookingEscalation.tenant_id == tenant(scope)).order_by(BookingEscalation.created_at.desc()).limit(200))).scalars()
    return [serialize(row) for row in rows]


@router.get("/settings", dependencies=[Depends(get_tenant_manager)])
async def settings(scope=Depends(get_tenant_scope), db=Depends(get_db)):
    spa = await db.get(SpaAccount, tenant(scope))
    config = spa.notification_settings or {}
    members = (await db.execute(select(User).where(User.tenant_id == spa.id, User.is_active.is_(True), User.role == UserRole.SPA_STAFF))).scalars()
    return {**Settings().model_dump(), **config.get("booking_escalation", {}),
            "staff_members": [{"id": str(u.id), "name": u.full_name or u.email} for u in members]}


@router.put("/settings", dependencies=[Depends(get_tenant_manager)])
async def save_settings(body: Settings, scope=Depends(get_tenant_scope), db=Depends(get_db)):
    spa = await db.get(SpaAccount, tenant(scope))
    allowed = set((await db.execute(select(User.id).where(User.tenant_id == spa.id, User.is_active.is_(True), User.role == UserRole.SPA_STAFF))).scalars())
    if not set(body.staff_user_ids).issubset(allowed):
        raise HTTPException(422, "Select active staff belonging to this business.")
    spa.notification_settings = {**(spa.notification_settings or {}), "booking_escalation": body.model_dump(mode="json")}
    await db.commit()
    return body


@router.patch("/{request_id}")
async def resolve(request_id: uuid.UUID, body: Resolution, scope=Depends(get_tenant_scope),
                  user=Depends(editor), db=Depends(get_db)):
    row = await service.resolve(db, tenant(scope), request_id, user.id, body.status,
        body.expected_version, body.note, body.provider_booking_id)
    return serialize(row)
