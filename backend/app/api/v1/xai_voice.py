"""
xAI Voice Agent webhook — the inbound entrypoint for SIP-trunked calls.

The number is attached to a Twilio Elastic SIP trunk whose origination URI is
sip.voice.x.ai, so Twilio never fetches TwiML and the <Gather> webhooks in
app/api/v1/telephony.py are bypassed entirely. xAI announces the call here
instead, and the call only becomes a conversation once we open the realtime
WebSocket for its call_id (see app/services/xai_realtime.py).

Tenancy, contact resolution and greeting text are reused from the <Gather> flow
so both paths route a spa's calls the same way and write the same CallLog rows.
"""
import logging
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_call_state_store, get_db, verify_xai_voice_signature
from app.api.v1.telephony import _resolve_inbound_target, _resolve_voice
from app.core.config import settings
from app.core.redis import redis_manager
from app.core.tenancy import TenantScope, scope_filter
from app.models import CallDirection, CallLog, CallStatus, Contact
from app.services.call_state import CallSession, CallStateStore
from app.services.grok_service import build_spa_prompt_context
from app.services.media_bridge import TwilioMediaBridge
from app.services.phone_numbers import normalize_phone_target
from app.services.xai_realtime import drive_call, xai_call_sid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telephony", tags=["telephony"])

INCOMING_CALL_EVENT = "realtime.call.incoming"


def _sip_header(headers: list[dict[str, str]], name: str) -> str | None:
    """Value of a SIP header, case-insensitively.

    The `From`/`To` values are the only identification an xAI voice webhook
    carries — there is no Twilio CallSid on this path — so `To` is what resolves
    which spa is answering.
    """
    target = name.lower()
    for header in headers:
        if isinstance(header, dict) and str(header.get("name", "")).lower() == target:
            return header.get("value")
    return None


@router.websocket("/media-stream/{call_sid}")
async def media_stream(websocket: WebSocket, call_sid: str) -> None:
    """Twilio <Connect><Stream> socket, bridged to xAI realtime.

    Only reachable for a call this app already set up: `voice_inbound` writes the
    CallSession (tenant persona, greeting, contact) to Redis before returning the
    <Stream> TwiML, so an unknown call_sid is a stray connection and is refused.
    Twilio cannot send auth headers on a stream, so that lookup is the check.
    """
    await websocket.accept()

    store = CallStateStore(redis_manager.client)
    session = await store.get(call_sid)
    if session is None:
        logger.warning("Media stream for unknown call %s; closing", call_sid)
        await websocket.close(code=1008)
        return

    logger.info("Media stream connected for call %s (%s)", call_sid, session.business_name)
    bridge = TwilioMediaBridge(call_sid, session, websocket)
    try:
        await bridge.run()
    except WebSocketDisconnect:
        logger.info("Caller disconnected from media stream %s", call_sid)
    except Exception:
        logger.exception("Media stream failed for call %s", call_sid)
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass  # already closed by the caller hanging up


@router.post("/xai/incoming", dependencies=[Depends(verify_xai_voice_signature)])
async def xai_incoming_call(
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> dict[str, str]:
    if not settings.XAI_VOICE_ENABLED:
        # Registered but switched off: refuse rather than half-answer a call we
        # are not going to drive.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="xAI voice handling is disabled (set XAI_VOICE_ENABLED=true).",
        )

    body = await request.json()
    event_type = body.get("type")
    if event_type != INCOMING_CALL_EVENT:
        # Acknowledge unknown event types so xAI does not retry them forever.
        logger.info("Ignoring xAI voice event %r", event_type)
        return {"status": "ignored"}

    data = body.get("data") or {}
    call_id = data.get("call_id")
    if not call_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Missing data.call_id")

    sip_headers = data.get("sip_headers") or []
    from_number = normalize_phone_target(_sip_header(sip_headers, "From")) or "unknown"
    to_number = normalize_phone_target(_sip_header(sip_headers, "To")) or "unknown"

    logger.info("xAI inbound call %s from %s to %s", call_id, from_number, to_number)

    spa, owner = await _resolve_inbound_target(db, to_number)
    voice = _resolve_voice(spa, owner)

    if spa:
        logger.info("Call %s routed to spa tenant %s (%s)", call_id, spa.id, spa.name)
    elif owner is None:
        logger.warning("xAI inbound call %s to unclaimed number %s", call_id, to_number)

    scope: TenantScope | None = None
    if spa:
        scope = TenantScope.for_tenant(spa.id)
    elif owner:
        scope = TenantScope.for_sales_workspace(owner.id)

    contact = None
    if scope:
        contact = (
            await db.execute(
                select(Contact).where(
                    scope_filter(scope, Contact), Contact.phone_number == from_number
                )
            )
        ).scalar_one_or_none()

    sid = xai_call_sid(call_id)
    existing = (
        await db.execute(select(CallLog).where(CallLog.twilio_call_sid == sid))
    ).scalar_one_or_none()
    if existing is not None:
        # Webhook delivery is at-least-once; a retry must not open a second
        # WebSocket for a call already being driven.
        logger.info("xAI call %s already accepted; ignoring duplicate delivery", call_id)
        return {"status": "duplicate", "call_id": call_id}

    call_log = CallLog(
        twilio_call_sid=sid,
        direction=CallDirection.INBOUND,
        status=CallStatus.IN_PROGRESS,
        from_number=from_number,
        to_number=to_number,
        contact_id=contact.id if contact else None,
        user_id=owner.id if owner else None,
        tenant_id=spa.id if spa else None,
        started_at=datetime.now(timezone.utc),
    )
    db.add(call_log)
    await db.commit()

    business_name = spa.name if spa else settings.APP_NAME
    if contact and contact.first_name:
        greeting = (
            f"Hello {contact.first_name}, thanks for calling back. "
            "How may I assist you today?"
        )
    else:
        greeting = f"Thank you for calling {business_name}. How may I assist you today?"

    session = CallSession(
        call_sid=call_id,
        direction="inbound",
        from_number=from_number,
        to_number=to_number,
        user_id=str(owner.id) if owner else None,
        tenant_id=str(spa.id) if spa else None,
        business_name=business_name,
        tenant_prompt=build_spa_prompt_context(spa, include_dashboard_facts=False) if spa else None,
        timezone=spa.timezone if spa else None,
        voice=voice,
    )
    if contact:
        session.entities["known_contact"] = {
            "id": str(contact.id),
            "name": contact.full_name,
        }
    # history[0] is the greeting the realtime driver speaks on connect, and the
    # first line of the saved transcript.
    session.add_turn("assistant", greeting)
    await state.create(session)

    # Answer the webhook immediately; the WebSocket is driven after the response
    # so xAI is not kept waiting on a session that lasts minutes.
    background.add_task(drive_call, call_id, session)

    return {"status": "accepted", "call_id": call_id}
