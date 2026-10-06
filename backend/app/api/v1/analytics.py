"""Tenant-filtered reporting.

`GET /api/v1/analytics` always describes exactly one scope. A spa user gets
their own spa and cannot ask for another; a super admin gets the 6DM sales
workspace by default and opts into a tenant with the `X-Tenant-Id` header that
backs the dashboard's tenant switcher.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_tenant_scope
from app.core.tenancy import TenantScope, call_log_scope_filter, scope_filter
from app.models import (
    Appointment,
    AppointmentStatus,
    CallDirection,
    CallLog,
    CallStatus,
    Contact,
)
from app.schemas import AnalyticsSummary, BookingVolume, CallVolume, SentimentBreakdown

router = APIRouter(prefix="/analytics", tags=["analytics"])

DEFAULT_WINDOW_DAYS = 30


def _count_if(condition):
    """COUNT of rows matching `condition`; the implicit NULL else is skipped."""
    return func.count(case((condition, 1)))


def _windowed(query: Select, column, start: datetime, end: datetime) -> Select:
    return query.where(column >= start, column < end)


@router.get("", response_model=AnalyticsSummary)
async def get_analytics(
    from_time: datetime | None = Query(None, description="Defaults to 30 days ago"),
    to_time: datetime | None = Query(None, description="Defaults to now"),
    scope: TenantScope = Depends(get_tenant_scope),
    db: AsyncSession = Depends(get_db),
) -> AnalyticsSummary:
    window_end = to_time or datetime.now(timezone.utc)
    window_start = from_time or (window_end - timedelta(days=DEFAULT_WINDOW_DAYS))

    sentiment = CallLog.ai_analysis["sentiment"].astext

    call_row = (
        await db.execute(
            _windowed(
                select(
                    func.count(CallLog.id).label("total"),
                    _count_if(CallLog.direction == CallDirection.INBOUND).label("inbound"),
                    _count_if(CallLog.direction == CallDirection.OUTBOUND).label("outbound"),
                    _count_if(CallLog.status == CallStatus.COMPLETED).label("completed"),
                    _count_if(
                        CallLog.status.in_(
                            [CallStatus.FAILED, CallStatus.NO_ANSWER, CallStatus.BUSY]
                        )
                    ).label("failed"),
                    func.avg(CallLog.duration_seconds).label("avg_duration"),
                    _count_if(sentiment == "positive").label("positive"),
                    _count_if(sentiment == "neutral").label("neutral"),
                    _count_if(sentiment == "negative").label("negative"),
                    _count_if(sentiment.is_(None)).label("unscored"),
                ).where(call_log_scope_filter(scope, CallLog)),
                CallLog.created_at,
                window_start,
                window_end,
            )
        )
    ).one()

    booking_row = (
        await db.execute(
            _windowed(
                select(
                    func.count(Appointment.id).label("total"),
                    _count_if(Appointment.status == AppointmentStatus.SCHEDULED).label("scheduled"),
                    _count_if(Appointment.status == AppointmentStatus.CONFIRMED).label("confirmed"),
                    _count_if(Appointment.status == AppointmentStatus.CANCELLED).label("cancelled"),
                    _count_if(Appointment.status == AppointmentStatus.COMPLETED).label("completed"),
                    _count_if(Appointment.status == AppointmentStatus.NO_SHOW).label("no_show"),
                ).where(scope_filter(scope, Appointment)),
                Appointment.created_at,
                window_start,
                window_end,
            )
        )
    ).one()

    contacts_total = (
        await db.execute(
            select(func.count(Contact.id)).where(scope_filter(scope, Contact))
        )
    ).scalar_one()

    return AnalyticsSummary(
        tenant_id=scope.tenant_id,
        scope="sales_workspace" if scope.is_sales_workspace else "spa",
        window_start=window_start,
        window_end=window_end,
        calls=CallVolume(
            total=call_row.total,
            inbound=call_row.inbound,
            outbound=call_row.outbound,
            completed=call_row.completed,
            failed=call_row.failed,
        ),
        bookings=BookingVolume(
            total=booking_row.total,
            scheduled=booking_row.scheduled,
            confirmed=booking_row.confirmed,
            cancelled=booking_row.cancelled,
            completed=booking_row.completed,
            no_show=booking_row.no_show,
        ),
        sentiment=SentimentBreakdown(
            positive=call_row.positive,
            neutral=call_row.neutral,
            negative=call_row.negative,
            unscored=call_row.unscored,
        ),
        average_call_duration_seconds=(
            float(call_row.avg_duration) if call_row.avg_duration is not None else None
        ),
        # Answered calls, not dialled ones: a no-answer never had the chance to
        # convert, so counting it would understate the agent.
        booking_conversion_rate=(
            round(booking_row.total / call_row.completed, 4)
            if call_row.completed
            else 0.0
        ),
        contacts_total=contacts_total,
    )
