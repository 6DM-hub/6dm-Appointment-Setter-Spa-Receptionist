"""Owner assistant and master approvals. No endpoint launches live outreach."""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from app.api.deps import get_current_user, get_db, get_super_admin, get_tenant_manager, get_tenant_scope
from app.core.tenancy import TenantScope
from app.models import Appointment, AppointmentStatus, Contact, SpaAccount, User, UserRole
from app.models.cara_manager import CaraCampaign, CaraPreference
from app.services import cara_manager as manager

router = APIRouter(prefix="/cara", tags=["Cara business manager"],
                   dependencies=[Depends(get_tenant_manager)])


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Ask(Body):
    message: str = Field(min_length=5, max_length=2000)
    test_mode: bool = True
    schedule: datetime | None = None


class Preferences(Body):
    discount_limit_pct: int = Field(default=0, ge=0, le=50)
    services_to_promote: list[str] = Field(default_factory=list, max_length=20)
    appointment_priority: Literal["earliest_available", "preferred_provider"] = "earliest_available"
    discount_excluded_weekdays: list[Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]] = Field(default_factory=list, max_length=7)
    promotion_until: datetime | None = None

    @field_validator("promotion_until")
    @classmethod
    def expiry_timezone(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Promotion expiry requires a timezone.")
        return value


class Approve(Body):
    expected_hash: str = Field(min_length=64, max_length=64)


class Edit(Body):
    expected_hash: str
    messages: dict[uuid.UUID, str] | None = None
    schedule: datetime | None = None

    @field_validator("messages")
    @classmethod
    def bounded_messages(cls, value):
        if value and any(manager.contains_sensitive_payment(text) or manager.sensitive_health_request(text) for text in value.values()):
            raise HTTPException(422, "Outreach messages must not contain raw card details or sensitive health information.")
        if value and (len(value) > 1000 or any(not text.strip() or len(text) > 1000 for text in value.values())):
            raise ValueError("Messages must contain 1–1000 characters.")
        return value


class Reply(Body):
    message: str = Field(min_length=1, max_length=1000)


class Book(Body):
    start: datetime


class StageTestBooking(Book):
    customer_name: str = Field(min_length=1, max_length=100)
    preferred_staff: str | None = Field(default=None, max_length=100)


class ConfirmTestBooking(Body):
    expected_fingerprint: str = Field(min_length=64, max_length=64)
    confirmation: str = Field(min_length=1, max_length=100)


class MasterAssignment(Body):
    is_business_master: bool


async def establishment_master(scope: TenantScope = Depends(get_tenant_scope), user=Depends(get_current_user)):
    if not manager.business_master(user, tenant(scope)):
        raise HTTPException(403, "Only this establishment's master account can review or approve its inbox.")
    return user


@router.get("/master-users")
async def master_users(scope: TenantScope = Depends(get_tenant_scope), platform=Depends(get_super_admin), db=Depends(get_db)):
    rows = (await db.execute(select(User).where(User.tenant_id == tenant(scope), User.role == UserRole.SPA_ADMIN))).scalars()
    return [{"id": str(u.id), "name": u.full_name, "is_active": u.is_active,
             "is_business_master": bool(u.is_business_master)} for u in rows]


@router.put("/master-users/{user_id}")
async def assign_master(user_id: uuid.UUID, body: MasterAssignment, scope: TenantScope = Depends(get_tenant_scope),
                        platform=Depends(get_super_admin), db=Depends(get_db)):
    tid = tenant(scope)
    target = (await db.execute(select(User).where(User.id == user_id, User.tenant_id == tid).with_for_update())).scalar_one_or_none()
    if not target or target.role != UserRole.SPA_ADMIN or (body.is_business_master and not target.is_active):
        raise HTTPException(422, "Select an active establishment administrator belonging to this establishment.")
    target.is_business_master = body.is_business_master
    manager.audit(db, tid, platform.id, "business_master_assigned" if body.is_business_master else "business_master_revoked", target_user_id=str(target.id))
    await db.commit()
    return {"id": str(target.id), "is_business_master": target.is_business_master}


def tenant(scope):
    if scope.tenant_id is None:
        raise HTTPException(403, "Select a spa tenant to use Ask Cara.")
    return scope.tenant_id


def campaign_json(row):
    return {"id": str(row.id), "status": row.status, "request": row.request,
            "proposal": row.proposal, "proposal_hash": row.proposal_hash,
            "approved_hash": row.approved_hash,
            "approved_by": str(row.approved_by) if row.approved_by else None,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None}


@router.get("/capabilities")
async def capabilities():
    return manager.CAPABILITIES


@router.get("/integration-status")
async def integration_status(scope: TenantScope = Depends(get_tenant_scope), db=Depends(get_db)):
    return await manager.integration_report(db, tenant(scope))


@router.get("/insights")
async def insights(scope: TenantScope = Depends(get_tenant_scope), db=Depends(get_db)):
    _, _, rows = await manager.evidence(db, tenant(scope))
    return {"customers": rows, "limitations": manager.CAPABILITIES}


@router.get("/preferences")
async def read_preferences(scope: TenantScope = Depends(get_tenant_scope), db=Depends(get_db)):
    return await manager.preferences(db, tenant(scope))


@router.put("/preferences")
async def write_preferences(body: Preferences, scope: TenantScope = Depends(get_tenant_scope),
                            user=Depends(get_tenant_manager), db=Depends(get_db)):
    tid = tenant(scope)
    spa = await db.get(SpaAccount, tid)
    names = {str(item.get("name")) for item in spa.services or []}
    if not set(body.services_to_promote).issubset(names):
        raise HTTPException(422, "Promoted services must be on the current service menu.")
    row = (await db.execute(select(CaraPreference).where(CaraPreference.tenant_id == tid))).scalar_one_or_none()
    if row is None:
        row = CaraPreference(tenant_id=tid, approved_by=user.id, value=body.model_dump(mode="json"))
        db.add(row)
    else:
        row.value, row.approved_by = body.model_dump(mode="json"), user.id
    manager.audit(db, tid, user.id, "owner_preferences_approved", preference_hash=manager.digest(row.value))
    await db.commit()
    return row.value


@router.delete("/preferences")
async def delete_preferences(scope: TenantScope = Depends(get_tenant_scope),
                             user=Depends(get_tenant_manager), db=Depends(get_db)):
    tid = tenant(scope)
    row = (await db.execute(select(CaraPreference).where(CaraPreference.tenant_id == tid))).scalar_one_or_none()
    if row:
        await db.delete(row)
    manager.audit(db, tid, user.id, "owner_preferences_deleted")
    await db.commit()
    return manager.DEFAULT_PREFERENCES


@router.post("/ask")
async def ask(body: Ask, scope: TenantScope = Depends(get_tenant_scope),
              user=Depends(get_tenant_manager), db=Depends(get_db)):
    row = await manager.prepare(db, tenant(scope), user.id, body.message, body.test_mode, body.schedule)
    return campaign_json(row)


@router.post("/interpret")
async def interpret(body: Ask, scope: TenantScope = Depends(get_tenant_scope), db=Depends(get_db)):
    return await manager.interpret(db, tenant(scope), body.message)


@router.get("/campaigns")
async def campaigns(scope: TenantScope = Depends(get_tenant_scope), db=Depends(get_db)):
    rows = (await db.execute(select(CaraCampaign).where(CaraCampaign.tenant_id == tenant(scope))
                            .order_by(CaraCampaign.created_at.desc()).limit(100))).scalars()
    return [campaign_json(row) for row in rows]


@router.get("/approvals")
async def inbox(scope: TenantScope = Depends(get_tenant_scope), master=Depends(establishment_master), db=Depends(get_db)):
    rows = (await db.execute(select(CaraCampaign).where(
        CaraCampaign.tenant_id == tenant(scope), CaraCampaign.status == "draft")
        .order_by(CaraCampaign.created_at))).scalars()
    return [campaign_json(row) for row in rows]


@router.patch("/campaigns/{campaign_id}")
async def edit(campaign_id: uuid.UUID, body: Edit, scope: TenantScope = Depends(get_tenant_scope),
               user=Depends(get_tenant_manager), db=Depends(get_db)):
    tid = tenant(scope)
    row = await manager.load_campaign(db, tid, campaign_id, True)
    if row.status == "executed":
        raise HTTPException(409, "Executed campaigns cannot be edited. Prepare a new proposal.")
    if body.expected_hash != row.proposal_hash:
        raise HTTPException(409, "Proposal changed. Reload it before editing.")
    plan = dict(row.proposal)
    if body.schedule:
        if body.schedule.tzinfo is None:
            raise HTTPException(422, "Schedule needs an explicit timezone.")
        plan["schedule"] = body.schedule.astimezone(timezone.utc).isoformat()
    if body.messages:
        known = {uuid.UUID(p["contact_id"]) for p in plan["audience"]}
        if not set(body.messages).issubset(known):
            raise HTTPException(422, "Message edits must reference the approved audience.")
        plan["audience"] = [
            dict(p, message=body.messages.get(uuid.UUID(p["contact_id"]), p["message"]))
            for p in plan["audience"]]
        # All campaign SMS must retain explicit opt-out instructions.
        if any("STOP" not in p["message"].upper() for p in plan["audience"]):
            raise HTTPException(422, "Every outreach message must include STOP opt-out instructions.")
    row.proposal, row.proposal_hash = plan, manager.digest(plan)
    row.status, row.approved_hash, row.approved_by, row.approved_at = "draft", None, None, None
    manager.audit(db, tid, user.id, "proposal_changed_approval_invalidated", row.id,
                  proposal_hash=row.proposal_hash)
    await db.commit()
    return campaign_json(row)


@router.post("/campaigns/{campaign_id}/approve")
async def approve(campaign_id: uuid.UUID, body: Approve, scope: TenantScope = Depends(get_tenant_scope),
                  master=Depends(establishment_master), db=Depends(get_db)):
    return campaign_json(await manager.approve(db, tenant(scope), master.id, campaign_id, body.expected_hash))


@router.post("/campaigns/{campaign_id}/execute-test")
async def execute(campaign_id: uuid.UUID, scope: TenantScope = Depends(get_tenant_scope),
                  user=Depends(get_tenant_manager), db=Depends(get_db)):
    return campaign_json(await manager.execute_test(db, tenant(scope), user.id, campaign_id))


@router.get("/campaigns/{campaign_id}/results")
async def results(campaign_id: uuid.UUID, scope: TenantScope = Depends(get_tenant_scope), db=Depends(get_db)):
    tid = tenant(scope)
    return await manager.results(db, tid, await manager.load_campaign(db, tid, campaign_id))


@router.post("/deliveries/{delivery_id}/reply-test")
async def reply(delivery_id: uuid.UUID, body: Reply, scope: TenantScope = Depends(get_tenant_scope),
                user=Depends(get_tenant_manager), db=Depends(get_db)):
    item = await manager.reply_test(db, tenant(scope), user.id, delivery_id, body.message)
    return {"id": str(item.id), "status": item.status}


@router.post("/deliveries/{delivery_id}/book-test")
async def book(delivery_id: uuid.UUID, body: Book, scope: TenantScope = Depends(get_tenant_scope),
               user=Depends(get_tenant_manager), db=Depends(get_db)):
    item = await manager.book_test(db, tenant(scope), user.id, delivery_id, body.start)
    return {"id": str(item.id), "status": item.status, "booking": item.booking}


@router.post("/deliveries/{delivery_id}/stage-test-booking")
async def stage_pipeline(delivery_id: uuid.UUID, body: StageTestBooking, scope: TenantScope = Depends(get_tenant_scope),
                         user=Depends(get_tenant_manager), db=Depends(get_db)):
    from app.services.campaign_booking import stage
    return await stage(db, tenant(scope), user.id, delivery_id, body.start, body.customer_name, body.preferred_staff)


@router.post("/deliveries/{delivery_id}/confirm-test-booking")
async def confirm_pipeline(delivery_id: uuid.UUID, body: ConfirmTestBooking, scope: TenantScope = Depends(get_tenant_scope),
                           user=Depends(get_tenant_manager), db=Depends(get_db)):
    from app.services.campaign_booking import confirm
    return await confirm(db, tenant(scope), user.id, delivery_id, body.expected_fingerprint, body.confirmation)


@router.post("/test-customers")
async def seed_test(scope: TenantScope = Depends(get_tenant_scope),
                    user=Depends(get_tenant_manager), db=Depends(get_db)):
    from app.services.campaign_booking import development_only
    development_only()
    tid = tenant(scope)
    spa = await db.get(SpaAccount, tid)
    menu = manager.menu_snapshot(spa)
    if not menu:
        raise HTTPException(422, "Add an active service with an exact USD price and duration before creating test history.")
    service = menu[0]
    ids = []
    for index, name in enumerate(("Alice", "Ben", "Casey")):
        phone = "+1999555010" + str(index)
        row = (await db.execute(select(Contact).where(Contact.tenant_id == tid, Contact.phone_number == phone))).scalar_one_or_none()
        if row:
            if (row.extra_metadata or {}).get("is_test_customer") is not True:
                raise HTTPException(409, "Test phone belongs to a non-test contact; refusing to change it.")
            ids.append(str(row.id))
            continue
        row = Contact(id=uuid.uuid4(), tenant_id=tid, phone_number=phone,
                      first_name="Cara Test " + name, extra_metadata={
                          "is_test_customer": True, "marketing_sms_opt_in": True,
                          "sms_opt_out": index == 2, "source": "explicit_test_fixture"})
        db.add(row)
        await db.flush()
        for days in (120, 240):
            start = manager.now_utc() - timedelta(days=days)
            db.add(Appointment(tenant_id=tid, contact_id=row.id, title=service["name"],
                               start_time=start, end_time=start + timedelta(minutes=service["duration_minutes"]),
                               status=AppointmentStatus.COMPLETED, booking_provider="test",
                               description="TEST history fixture; no actual customer visit or revenue"))
        ids.append(str(row.id))
    manager.audit(db, tid, user.id, "test_history_created", contacts=ids)
    await db.commit()
    return {"contact_ids": ids, "source": "test_fixture", "message": "Only simulated history was created. No external messages or bookings."}
