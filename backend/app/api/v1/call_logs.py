"""Call history.

Mounted twice: `/api/v1/call-logs` (original) and `/api/v1/calls` (the name in
the multi-tenant architecture). Same handlers, so the tenant filter cannot
drift between them.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_db, get_tenant_scope
from app.core.tenancy import TenantScope, call_log_scope_filter, owns
from app.models import CallDirection, CallLog, CallStatus
from app.schemas import CallLogRead, Page

async def list_call_logs(
    page: int = Query(1, ge=1),
    size: int = Query(25, ge=1, le=100),
    direction: CallDirection | None = None,
    status_filter: CallStatus | None = Query(None, alias="status"),
    contact_id: uuid.UUID | None = None,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> Page[CallLogRead]:
    # Transcripts are the most sensitive thing in the system; this filter is
    # what stops one spa reading another's calls.
    query = select(CallLog).where(call_log_scope_filter(scope, CallLog))
    if direction and scope.tenant_id is None:
        query = query.where(CallLog.direction == direction)
    if status_filter:
        query = query.where(CallLog.status == status_filter)
    if contact_id:
        query = query.where(CallLog.contact_id == contact_id)

    total = (
        await db.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            query.options(selectinload(CallLog.contact))
            .order_by(CallLog.created_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
    ).scalars().all()
    return Page(
        items=[CallLogRead.model_validate(r) for r in rows],
        total=total, page=page, size=size,
    )


async def get_call_log(
    call_log_id: uuid.UUID,
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> CallLog:
    call_log = await db.get(
        CallLog, call_log_id, options=[selectinload(CallLog.contact)]
    )
    if not call_log or not owns(scope, call_log):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call log not found")
    if scope.tenant_id is not None and call_log.direction is not CallDirection.INBOUND:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Call log not found")
    return call_log


def _build_router(prefix: str) -> APIRouter:
    """Bind the handlers above onto a router at `prefix`.

    Registering the same functions twice, rather than including one router into
    another, keeps both mounts on identical code — and sidesteps FastAPI's
    refusal to include a router with an empty prefix *and* an empty route path.
    """
    router = APIRouter(prefix=prefix, tags=["calls"])
    router.add_api_route(
        "", list_call_logs, methods=["GET"], response_model=Page[CallLogRead]
    )
    router.add_api_route(
        "/{call_log_id}", get_call_log, methods=["GET"], response_model=CallLogRead
    )
    return router


router = _build_router("/call-logs")
alias_router = _build_router("/calls")
