"""
Twilio voice webhooks and outbound call trigger.

Pipeline (100% Twilio + Grok, no third-party STT/TTS):
  1. Caller speaks -> Twilio <Gather input="speech"> transcribes it (Twilio ASR)
     and POSTs SpeechResult to /voice/respond.
  2. We run Grok structured extraction -> auto-booking engine -> Grok reply.
  3. Reply is spoken back via Twilio <Say> (Amazon Polly voice), then we
     <Gather> again for the caller's next turn.
"""
import inspect
import logging
import time
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException
from twilio.jwt.access_token import AccessToken
from twilio.twiml.voice_response import Connect, Gather, VoiceResponse

try:
    from twilio.jwt.access_token import VoiceGrant
except ImportError:  # Twilio SDK versions newer than the old import path.
    from twilio.jwt.access_token.grants import VoiceGrant

from app.api.deps import (
    get_call_state_store,
    get_current_user,
    get_db,
    get_super_admin,
    verify_twilio_signature,
)
from app.core.config import settings
from app.core.tenancy import TenantScope, scope_filter
from app.models import (
    CallDirection,
    CallLog,
    CallStatus,
    Contact,
    SpaAccount,
    User,
    UserRole,
    VoiceEngine,
)
from app.schemas import OutboundCallRequest, OutboundCallResponse
from app.services.appointment_booking_service import (
    CANCELLATION_KEPT_MARKER,
    BookingOutcome,
    apply_booking_result,
    cancel_booking,
    confirm_booking,
    stage_booking,
)
from app.services import booking_change_guard as booking_guard
from app.services.booking_state import (
    contains_unauthorized_availability_claim,
    get_draft,
    is_affirmative,
    looks_like_unverified_booking_success,
    mark_read_back,
    mentions_scheduling,
    record_pure_confirmation,
    refuses_offer,
    start_new_intent,
    utterance_modifies_booking,
)
from app.services.business_hours import resolve_timezone
from app.services.call_finalization import finalize_gather_call
from app.services.call_state import CallSession, CallStateStore
from app.services.caller_identity import persist_caller_identity
from app.services.grok_service import build_spa_prompt_context, grok_service
from app.services.receptionist_identity import incoming_greeting
from app.services.phone_numbers import normalize_phone_target
from app.services.twilio_service import twilio_service
from app.services.twilio_sync import repair_unscoped_call_logs, sync_calls

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telephony", tags=["telephony"])

TWIML_MEDIA_TYPE = "application/xml"
INTENT_CONFIDENCE_THRESHOLD = 0.55
ACTIONABLE_INTENTS = {"schedule", "reschedule", "cancel"}
GOODBYE_MARKERS = (
    "goodbye",
    "good bye",
    "bye now",
    "have a great day",
    "have a wonderful day",
    "have a good day",
    "we'll see you then",
    "see you then",
    "take care",
)
# The agent asking the caller to approve the details it has just recited.
BOOKING_CONFIRMATION_REQUEST = re.compile(
    r"\b(would you like me to|shall i|should i|can i|may i|do you want me to|"
    r"just to confirm|to confirm|is that (right|correct)|does that (work|sound))\b",
    re.IGNORECASE,
)


def _asks_for_confirmation(reply: str) -> bool:
    """True when the reply is a request for the caller's go-ahead.

    Gates `mark_read_back`, so a later bare "yes" can only ever commit details
    the caller has actually heard read back to them.
    """
    text = (reply or "").strip()
    return text.endswith("?") and bool(BOOKING_CONFIRMATION_REQUEST.search(text))


def _authoritative_reply(session: CallSession, reply: str) -> str:
    """Prevent model-only booking claims in the Twilio text pipeline."""
    booking_is_persisted = (
        session.booking_status in {"booked", "rescheduled"}
        and session.appointment_id is not None
    )
    if not booking_is_persisted and looks_like_unverified_booking_success(reply):
        logger.error(
            "Blocked unsupported booking confirmation for call %s: status=%s appointment_id=%s",
            session.call_sid,
            session.booking_status,
            session.appointment_id,
        )
        return "I could not complete that appointment. Would you like to try another time?"
    return reply


def _note_gratuity_discussion(session: CallSession, utterance: str | None) -> None:
    """Prevent the legacy voice path from repeating a policy it just answered."""
    from app.services.gratuity import mentions_gratuity

    if mentions_gratuity(utterance):
        session.entities["gratuity_discussed"] = True


def _with_gratuity_reminder(session: CallSession, reply: str, result: object | None) -> str:
    """Apply the same verified reminder policy to the legacy Twilio voice path."""
    from app.services.gratuity import (
        AFTER_BOOKING_CONFIRMATION,
        DURING_CLOSING,
        booking_card_allows_reminder,
        fee_may_be_spoken,
        normalize_gratuity_policy,
        reminder_message,
    )

    entities = session.entities
    policy = normalize_gratuity_policy(entities.get("gratuity_policy"))
    outcome = getattr(result, "outcome", None)
    appointment = getattr(result, "appointment", None)
    external_id = getattr(appointment, "external_booking_id", None)
    successful = (
        outcome in {BookingOutcome.BOOKED, BookingOutcome.RESCHEDULED}
        and appointment is not None
        and external_id
        and booking_card_allows_reminder(getattr(appointment, "card_status", None))
    )

    if (
        successful
        and not entities.get("gratuity_reminder_spoken")
        and not entities.get("gratuity_discussed")
    ):
        key = f"{external_id}:{session.confirmed_datetime or ''}"
        message = reminder_message(policy, selection_key=key)
        if message and policy.get("timing") == DURING_CLOSING:
            entities["pending_gratuity_reminder"] = {
                "confirmation_key": key,
                "message": message,
                "fee_included": fee_may_be_spoken(policy),
            }
        elif message and policy.get("timing") == AFTER_BOOKING_CONFIRMATION:
            entities["gratuity_reminder_spoken"] = True
            entities.pop("pending_gratuity_reminder", None)
            question = re.search(
                r"\s*is there anything else i can help you with today\?\s*$",
                reply,
                re.IGNORECASE,
            )
            if question:
                reply = reply[: question.start()].rstrip() + " " + message + " " + question.group().strip()
            else:
                reply = reply.rstrip() + " " + message
            logger.info(
                "call %s: gratuity reminder spoken timing=%s fee_included=%s",
                session.call_sid,
                AFTER_BOOKING_CONFIRMATION,
                fee_may_be_spoken(policy),
            )

    pending = entities.get("pending_gratuity_reminder")
    if (
        isinstance(pending, dict)
        and pending.get("message")
        and not entities.get("gratuity_reminder_spoken")
        and not entities.get("gratuity_discussed")
        and _is_closing_reply(reply)
    ):
        entities.pop("pending_gratuity_reminder", None)
        entities["gratuity_reminder_spoken"] = True
        reply = str(pending["message"]).strip() + " " + reply.lstrip()
        logger.info(
            "call %s: gratuity reminder spoken timing=%s fee_included=%s",
            session.call_sid,
            DURING_CLOSING,
            bool(pending.get("fee_included")),
        )
    return reply


def _post_booking_gate(session: CallSession, speech: str, intent, tz) -> tuple[str | None, object | None]:
    """Protect a booking made earlier on this call (legacy text loop).

    The same shared, industry-neutral rules as the realtime path: "keep it"
    restores the booking and stages nothing; whether a later request changes
    the booking or adds another is decided from the caller's words, and an
    unclear request is asked about first. Returns (fixed reply or None, the
    intent to stage or None).
    """
    if not booking_guard.booking_on_record(session) or not booking_guard.caller_spoke_since_booking(session):
        return None, intent
    turn = booking_guard.current_turn(session)
    if booking_guard.keep_request(session, speech, tz) and (
        booking_guard.something_pending(session)
        or booking_guard.after_completed_booking(session, turn)
    ):
        return booking_guard.keep_existing_booking(
            session, tz, turn=turn, call_sid=session.call_sid
        ), None
    if (
        intent is None
        or intent.intent not in {"schedule", "reschedule"}
        or not booking_guard.after_completed_booking(session, turn)
    ):
        return None, intent
    args = {
        "requested_start_iso": getattr(intent, "requested_start_iso", None),
        "service_description": getattr(intent, "service_description", None),
        "guest_name": getattr(intent, "guest_name", None),
        "preferred_staff": getattr(intent, "preferred_staff", None),
        "earliest": getattr(intent, "earliest", None),
    }
    decision = booking_guard.decide(
        session, speech, model_operation=intent.intent, appointment_id=None,
        args={k: v for k, v in args.items() if v}, turn=turn, tz=tz, call_sid=session.call_sid,
    )
    if decision == "keep":
        return booking_guard.keep_existing_booking(
            session, tz, turn=turn, call_sid=session.call_sid
        ), None
    if decision in {"ask", "wait"}:
        return booking_guard.change_question(session, tz), None
    if decision == "unchanged":
        return None, None
    if decision == "additional":
        start_new_intent(session)
        booking_guard.mark_additional_booking(session, tz)
        session.booking_status = "collecting_details"
        return None, intent.model_copy(update={"intent": "schedule"})
    return None, intent.model_copy(update={"intent": "reschedule"})


def _authoritative_availability_reply(session: CallSession, reply: str) -> str:
    """Availability-side counterpart to `_authoritative_reply`.

    Scoped to Square tenants (`session.entities["booking_provider"] ==
    "square"`, set once a booking call has resolved the tenant's adapter):
    that is the only provider this codebase currently sources
    `AvailabilityVerdict.slot` from, so enforcing this elsewhere would block
    perfectly normal replies from a Google-Calendar/local tenant that has no
    such state to check against.
    """
    if session.entities.get("booking_provider") != "square":
        return reply
    tz = resolve_timezone(session.timezone)
    if contains_unauthorized_availability_claim(reply, session, tz):
        logger.error(
            "Blocked unsupported availability claim for call %s: reply=%r",
            session.call_sid,
            reply,
        )
        return "Let me check that time for you."
    return reply


def _is_closing_reply(reply: str) -> bool:
    """True only when the agent is genuinely signing off.

    Matching the markers as substrings anywhere in the reply hangs the call up
    mid-conversation on perfectly normal lines like "before I say goodbye, can
    I confirm your number?". So a marker only counts when it ends the final
    sentence, and a reply that closes on a question is never a sign-off — the
    agent is still waiting for an answer.

    Biased towards *not* hanging up: a missed sign-off just means one more
    <Gather>, which the retry guard in `voice_respond` already closes out,
    whereas a false positive drops a live caller.
    """
    text = reply.strip().lower()
    if not text or text.endswith("?"):
        return False

    sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
    if not sentences:
        return False

    tail = sentences[-1].strip(" \t,;:-'\"")
    return any(tail.endswith(marker) for marker in GOODBYE_MARKERS)

_TWILIO_STATUS_MAP = {
    "queued": CallStatus.QUEUED,
    "initiated": CallStatus.QUEUED,
    "ringing": CallStatus.RINGING,
    "in-progress": CallStatus.IN_PROGRESS,
    "answered": CallStatus.IN_PROGRESS,
    "completed": CallStatus.COMPLETED,
    "busy": CallStatus.BUSY,
    "failed": CallStatus.FAILED,
    "no-answer": CallStatus.NO_ANSWER,
    "canceled": CallStatus.CANCELLED,
}


def _twiml(vr: VoiceResponse) -> Response:
    return Response(content=str(vr), media_type=TWIML_MEDIA_TYPE)


def _gather_twiml(prompt: str, voice: str) -> VoiceResponse:
    """Speak `prompt` via Twilio TTS, then gather the caller's next utterance
    via Twilio's built-in speech recognition."""
    vr = VoiceResponse()
    
    # Use full absolute URL to avoid route-resolution issues across proxies
    action_url = (
    f"{settings.PUBLIC_BASE_URL}"
    f"{settings.API_V1_PREFIX}/telephony/voice/respond"
)

    gather = Gather(
        input="speech",
        action=action_url,
        method="POST",
        speech_timeout="auto",
        language=settings.DEFAULT_TWIML_LANGUAGE,
        action_on_empty_result=True,
    )
    gather.say(prompt, voice=voice)
    vr.append(gather)

    # Fallback if gather times out or no speech is heard
    vr.say("Are you still there?", voice=voice)
    vr.redirect(action_url, method="POST")
    return vr


def _media_stream_twiml(call_sid: str) -> VoiceResponse:
    """Hand the call's audio to our realtime bridge instead of <Gather>/<Say>.

    <Connect><Stream> is bidirectional: Twilio pushes caller audio to the socket
    and plays back whatever we send. No greeting is spoken here — the bridge
    speaks it over the stream once xAI's session is configured, so the wording
    still comes from the tenant's own prompt.
    """
    vr = VoiceResponse()
    connect = Connect()
    connect.stream(
        url=(
            f"{settings.public_ws_base_url}{settings.API_V1_PREFIX}"
            f"/telephony/media-stream/{call_sid}"
        )
    )
    vr.append(connect)
    return vr


async def _maybe_await(value):
    """Support both async and sync ORM calls in tests and runtime code."""
    if inspect.isawaitable(value):
        return await value
    return value


def _browser_identity_to_tenant(raw: str | None) -> uuid.UUID | None:
    """Parse browser Twilio identity into the tenant UUID."""
    if not raw:
        return None

    value = raw.strip()

    # Current Twilio-safe format:
    # tenant_<32-character UUID hex>
    if value.startswith("tenant_"):
        candidate = value[len("tenant_"):]
        try:
            return uuid.UUID(hex=candidate)
        except ValueError:
            return None

    # Legacy formats kept for backward compatibility.
    for prefix in ("client:tenant:", "tenant:"):
        if value.startswith(prefix):
            candidate = value[len(prefix):]
            candidate = candidate.split(":", 1)[0]
            try:
                return uuid.UUID(candidate)
            except ValueError:
                return None

    return None


async def _resolve_browser_tenant(
    user: User,
    db: AsyncSession,
    *,
    x_tenant_id: uuid.UUID | None,
) -> SpaAccount:
    """Require a tenant-scoped spa for browser voice tokens and browser calls."""
    if user.role in {UserRole.SUPER_ADMIN, UserRole.SPA_ADMIN, UserRole.SPA_STAFF}:
        if user.role == UserRole.SUPER_ADMIN:
            spa_id = x_tenant_id
            if spa_id is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    detail="X-Tenant-Id header is required for browser voice testing.",
                )
        else:
            spa_id = user.tenant_id
            if spa_id is None:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    detail="This account is not attached to a spa tenant.",
                )
            if x_tenant_id is not None and x_tenant_id != spa_id:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    detail="You may only access your own spa account.",
                )

        spa = await _maybe_await(db.get(SpaAccount, spa_id))
        if spa is None or not spa.is_active:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Spa account not found")
        return spa

    raise HTTPException(
        status.HTTP_403_FORBIDDEN,
        detail="Browser voice testing requires a spa account role.",
    )


def _resolve_voice(spa: SpaAccount | None, owner: User | None) -> str:
    """Per-tenant Twilio <Say> voice, with the workspace setting as fallback."""
    if spa and spa.twiml_voice:
        return spa.twiml_voice
    if owner and isinstance(owner.workspace_settings, dict):
        return owner.workspace_settings.get("twiml_voice", settings.DEFAULT_TWIML_VOICE)
    return settings.DEFAULT_TWIML_VOICE


async def _resolve_inbound_target(
    db: AsyncSession, to_number: str
) -> tuple[SpaAccount | None, User | None]:
    """Dispatch an inbound call to the tenant that owns the dialed number.

    Spa accounts are checked first: an inbound B2C call is the Spa Receptionist
    product, and the `To` number is the only thing that identifies which spa is
    answering. Falling through to a workspace user keeps 6DM's own inbound
    number working.

    This lookup is the whole of per-spa routing — onboarding a spa means
    inserting a `SpaAccount` row with its number, nothing more.
    """
    spa = (
        await db.execute(
            select(SpaAccount).where(
                SpaAccount.twilio_phone_number == to_number,
                SpaAccount.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()
    if spa:
        return spa, None

    owner = (
        await db.execute(select(User).where(User.twilio_phone_number == to_number))
    ).scalar_one_or_none()
    if owner:
        return None, owner

    # Single-tenant dev fallback: if exactly one user exists, treat them as owner
    users = (await db.execute(select(User).limit(2))).scalars().all()
    return None, (users[0] if len(users) == 1 else None)


# --------------------------------------------------------------------------- #
# Inbound call entrypoint
# --------------------------------------------------------------------------- #
@router.get("/voice/token")
async def browser_voice_token(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    x_tenant_id: uuid.UUID | None = Header(
        default=None,
        alias="X-Tenant-Id",
        description="Tenant being tested; spa users are pinned to their own tenant.",
    ),
) -> dict[str, str | int | None]:
    """Issue a short-lived Twilio Voice SDK token for browser testing.

    The token is tenant-scoped and only contains a Voice grant plus the tenant
    identity; the browser never receives Twilio account credentials or secret
    values.
    """
    spa = await _resolve_browser_tenant(user, db, x_tenant_id=x_tenant_id)

    required = (
        settings.TWILIO_ACCOUNT_SID,
        settings.TWILIO_API_KEY_SID,
        settings.TWILIO_API_KEY_SECRET,
        settings.TWILIO_TWIML_APP_SID,
    )
    if not all(required):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Browser voice testing is not configured on the server.",
        )

    identity = f"tenant:{spa.id}"
    token = AccessToken(
        settings.TWILIO_ACCOUNT_SID,
        settings.TWILIO_API_KEY_SID,
        settings.TWILIO_API_KEY_SECRET,
        identity=identity,
        ttl=settings.TWILIO_BROWSER_TOKEN_TTL_SECONDS,
    )
    grant_kwargs = {
        "outgoing_application_sid": settings.TWILIO_TWIML_APP_SID,
        "incoming_allow": True,
    }
    if "outgoing_allow" in inspect.signature(VoiceGrant).parameters:
        grant_kwargs["outgoing_allow"] = True
    grant = VoiceGrant(**grant_kwargs)
    token.add_grant(grant)

    logger.info(
        "Browser voice token issued tenant=%s spa=%s role=%s",
        spa.id,
        spa.name,
        user.role.value,
    )

    return {
        "token": token.to_jwt(),
        "tenant_id": str(spa.id),
        "identity": identity,
        "expires_in": settings.TWILIO_BROWSER_TOKEN_TTL_SECONDS,
        "spa_name": spa.name,
        "voice_engine": spa.voice_engine.value if spa.voice_engine is not None else None,
    }


@router.post("/voice/browser-test", dependencies=[Depends(verify_twilio_signature)])
async def voice_browser_test(
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    tenant_id: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    """Route a browser-originated Twilio call through the same realtime flow.

    The browser identity is the tenant identity from its access token; we resolve
    the spa from that, create the normal call session, and then feed the call
    straight into the existing media-stream + xAI realtime path.
    """
    browser_tenant_id = tenant_id or _browser_identity_to_tenant(From)
    if browser_tenant_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="A tenant-scoped browser identity is required.",
        )

    tenant_uuid = browser_tenant_id if isinstance(browser_tenant_id, uuid.UUID) else uuid.UUID(str(browser_tenant_id))
    spa = await _maybe_await(db.get(SpaAccount, tenant_uuid))
    if spa is None or not spa.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Spa account not found")

    from_number = "+12025550123"
    to_number = "browser-test"
    logger.info(
        "Browser test call %s routed to spa tenant %s (%s) source=browser_test voice_engine=%s",
        CallSid,
        spa.id,
        spa.name,
        spa.voice_engine.value if spa.voice_engine is not None else None,
    )

    session = CallSession(
        call_sid=CallSid,
        direction="inbound",
        from_number=from_number,
        to_number=to_number,
        user_id=None,
        tenant_id=str(spa.id),
        business_name=spa.name,
        tenant_prompt=build_spa_prompt_context(spa),
        timezone=spa.timezone,
        voice=_resolve_voice(spa, None),
    )
    session.entities["call_source"] = "browser_test"
    if spa and spa.twiml_voice:
        session.voice = spa.twiml_voice

    greeting = incoming_greeting(spa.name)
    session.add_turn("assistant", greeting)
    await state.create(session)

    logger.info("Browser call %s using xAI realtime bridge", CallSid)
    return _twiml(_media_stream_twiml(CallSid))

    return _twiml(_gather_twiml(greeting, session.voice))


@router.post("/voice/inbound", dependencies=[Depends(verify_twilio_signature)])
async def voice_inbound(
    CallSid: str = Form(...),
    From: str = Form(...),
    To: str = Form(...),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    from_number = normalize_phone_target(From) or From
    to_number = normalize_phone_target(To) or To
    logger.info("Inbound call %s from %s to %s", CallSid, from_number, to_number)

    existing_log = (
        await db.execute(select(CallLog).where(CallLog.twilio_call_sid == CallSid))
    ).scalar_one_or_none()
    if existing_log is not None:
        logger.info("Duplicate inbound Twilio webhook ignored for call %s", CallSid)
        spa = await db.get(SpaAccount, existing_log.tenant_id) if existing_log.tenant_id else None
        if spa is not None and spa.voice_engine is VoiceEngine.XAI_REALTIME:
            return _twiml(_media_stream_twiml(CallSid))
        session = await state.get(CallSid)
        voice = session.voice if session else _resolve_voice(spa, None)
        return _twiml(_gather_twiml("Thank you for calling. How may I help you today?", voice))

    spa, owner = await _resolve_inbound_target(db, to_number)
    voice = _resolve_voice(spa, owner)

    if spa:
        logger.info(
            "Call %s routed to spa tenant %s (%s) voice_engine=%s twiml_voice=%s",
            CallSid,
            spa.id,
            spa.name,
            spa.voice_engine.value,
            voice,
        )
    elif owner is None:
        logger.warning("Inbound call %s to unclaimed number %s", CallSid, To)

    # Everything this call creates lands in exactly one scope.
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

    call_log = CallLog(
        twilio_call_sid=CallSid,
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

    session = CallSession(
        call_sid=CallSid,
        direction="inbound",
        from_number=from_number,
        to_number=to_number,
        user_id=str(owner.id) if owner else None,
        tenant_id=str(spa.id) if spa else None,
        business_name=spa.name if spa else settings.APP_NAME,
        # Rendered once here from the tenant's row (prompt, service menu, staff,
        # opening hours) and reused for every turn of this call.
        tenant_prompt=build_spa_prompt_context(spa) if spa else None,
        timezone=spa.timezone if spa else None,
        voice=voice,
    )
    if contact:
        session.entities["known_contact"] = {"id": str(contact.id), "name": contact.full_name}

    greeting = incoming_greeting(
        session.business_name, returning=bool(session.entities.get("known_contact"))
    )

    session.add_turn("assistant", greeting)
    await state.create(session)

    # Inbound calls have no status_callback unless the receiving number is
    # configured with one in the Twilio console; attach one to this specific
    # call so `voice_status` fires on completion and flushes the transcript
    # accumulated in `session` into `call_logs.transcript`.
    await twilio_service.set_inbound_status_callback(CallSid)

    # Tenants on the realtime engine get their audio bridged to xAI instead of
    # transcribed by Twilio. Everyone else keeps the <Gather> pipeline.
    if spa is not None and spa.voice_engine is VoiceEngine.XAI_REALTIME:
        logger.info("Call %s using xAI realtime bridge", CallSid)
        return _twiml(_media_stream_twiml(CallSid))

    return _twiml(_gather_twiml(greeting, voice))


# --------------------------------------------------------------------------- #
# Conversation turn: Twilio posts caller speech (its own ASR), Grok extracts
# intent, the booking engine runs, then Grok's reply is spoken via <Say>.
# --------------------------------------------------------------------------- #
@router.post("/voice/respond", dependencies=[Depends(verify_twilio_signature)])
async def voice_respond(
    CallSid: str = Form(...),
    SpeechResult: str | None = Form(None),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    session = await state.get(CallSid)

    if session is None:
        vr = VoiceResponse()
        vr.say("I'm sorry, something went wrong. Please call back. Goodbye.")
        vr.hangup()
        return _twiml(vr)

    # Infinite loop guard for silent / unclear responses
    if not SpeechResult:
        retry_count = session.entities.get("retry_count", 0) + 1
        session.entities["retry_count"] = retry_count
        await state.save(session)

        if retry_count >= 2:
            vr = VoiceResponse()
            vr.say("I'm having trouble hearing you. Please try calling back later. Goodbye.", voice=session.voice)
            vr.hangup()
            await finalize_gather_call(db, state, CallSid)
            return _twiml(vr)

        return _twiml(_gather_twiml("I didn't catch that. Could you say it again?", session.voice))

    # Reset retry count upon successfully receiving speech
    session.entities["retry_count"] = 0
    turn_started_at = time.perf_counter()
    session.add_turn("user", SpeechResult)
    if booking_guard.USER_TURN_COUNT in session.entities:
        # A call that started on the realtime path keeps one turn count.
        session.entities[booking_guard.USER_TURN_COUNT] = int(
            session.entities[booking_guard.USER_TURN_COUNT]
        ) + 1
    _note_gratuity_discussion(session, SpeechResult)
    tz = resolve_timezone(session.timezone)
    turn = booking_guard.current_turn(session)

    # Structured extraction + booking BEFORE the agent replies.
    #
    # Extraction is a full model round-trip on the caller's critical path, so
    # it is skipped entirely on turns that cannot affect a booking ("hello",
    # "thanks", "sorry, can you repeat that") — roughly half the turns of a
    # real call, each saving about a second of dead air.
    caller_affirmed = is_affirmative(SpeechResult)
    draft_before = get_draft(session)
    relevant = (
        mentions_scheduling(SpeechResult)
        or caller_affirmed
        or draft_before.is_complete
    )
    intent = (
        await grok_service.extract_appointment_intent(session) if relevant else None
    )
    extraction_ms = (time.perf_counter() - turn_started_at) * 1000
    logger.info(
        "TURN LATENCY call=%s VAD END: 0 ms STT: 0 ms "
        "LLM FIRST TOKEN: %.0f ms extraction_skipped=%s",
        CallSid,
        extraction_ms,
        not relevant,
    )
    if intent and (intent.caller_name or intent.caller_email):
        await persist_caller_identity(db, session, intent.caller_name, intent.caller_email)

    result = None
    actionable = bool(
        intent
        and intent.confidence >= INTENT_CONFIDENCE_THRESHOLD
        and intent.intent in ACTIONABLE_INTENTS
    )
    # A booking made earlier on this call is protected before anything is
    # staged or confirmed: "keep it" keeps it, unclear requests are asked.
    guard_reply, guarded_intent = _post_booking_gate(
        session, SpeechResult, intent if actionable else None, tz
    )
    # The change/add/keep question and "it stays as it is" are statements:
    # they never count as reading a proposal back for a later "yes".
    guard_statement = guard_reply is not None
    cancel_handled = False
    if session.entities.get(booking_guard.PENDING_CANCELLATION):
        # The caller is answering CARA's own cancellation question. Only a
        # plain yes to exactly that question cancels, whatever was extracted.
        decision = booking_guard.cancellation_decision(session)
        if decision == "confirmed":
            result = await cancel_booking(db, session, intent)
            cancel_handled = True
        else:
            session.entities.pop(booking_guard.PENDING_CANCELLATION, None)
            if decision == "kept":
                guard_reply = booking_guard.refused_cancellation_speech(session, tz)
                guard_statement = True
                cancel_handled = True
    if (
        not cancel_handled
        and intent and intent.intent == "cancel" and intent.confidence >= INTENT_CONFIDENCE_THRESHOLD
    ):
        if guard_reply is None:
            result = await cancel_booking(db, session, intent)
            cancel_handled = True
    if cancel_handled and result is not None:
        message = result.message or ""
        if CANCELLATION_KEPT_MARKER in message:
            # The caller's words keep the appointment; the extraction was wrong.
            guard_reply = booking_guard.refused_cancellation_speech(session, tz)
            guard_statement = True
        elif booking_guard.CANCELLATION_QUESTION_MARKER in message:
            # Step one: CARA asks its own question, verbatim.
            guard_reply = message.replace(booking_guard.CANCELLATION_QUESTION_MARKER, "").strip()
            guard_statement = True
        elif result.outcome is BookingOutcome.ERROR:
            guard_reply = (
                booking_guard.uncertain_change_speech(session, tz)
                if session.entities.get("booking_reconciliation_required")
                else "I wasn't able to cancel that appointment, so it is still booked. "
                "May a team member call you to help with it?"
            )
            guard_statement = True
    elif guarded_intent is not None and guard_reply is None and not cancel_handled:
        # Staging only. The caller naming a date is a request, not consent —
        # booking it here is what produced a row for every date they floated.
        result = await stage_booking(db, session, guarded_intent)

    # "No" / "no, don't book that" withdraws what was offered and every consent
    # for it, so no later yes (to another question) can book it.
    if guard_reply is None and refuses_offer(SpeechResult):
        booking_guard.retire_refused_offer(session, tz, turn=turn, call_sid=CallSid)

    # Commit only on an explicit "yes" to details the agent has already read
    # back. Detected from the caller's own words rather than left to the model,
    # so no prompt wording can talk the system into writing early.
    draft = get_draft(session)
    if (
        guard_reply is None
        and caller_affirmed
        and not utterance_modifies_booking(SpeechResult)
        and draft.is_complete
        and draft.read_back
        and record_pure_confirmation(session, SpeechResult)
    ):
        # An additional appointment is only booked after the caller heard
        # that it IS additional and answered on a later turn.
        gate = booking_guard.additional_confirm_gate(session, turn)
        if gate is None:
            result = await confirm_booking(db, session)
        elif gate[0] == "speak":
            guard_reply = gate[1]

    if result is not None:
        apply_booking_result(session, result)
        session.entities["last_booking_outcome"] = result.outcome.value
        if result.outcome != BookingOutcome.SKIPPED:
            session.add_turn("system", result.to_system_message())
        if result.appointment:
            session.entities["active_appointment_id"] = str(result.appointment.id)

    if guard_reply is not None:
        reply = guard_reply
    else:
        reply = await grok_service.generate_voice_response(session)
        additional = booking_guard.additional_marker(session)
        if (
            additional is not None
            and not additional.get("notice_spoken")
            and result is not None
            and result.outcome in {BookingOutcome.DRAFT, BookingOutcome.CONFLICT}
        ):
            reply = f"{additional['notice']} {reply}"
            booking_guard.note_additional_notice_spoken(session, turn)
    total_ms = (time.perf_counter() - turn_started_at) * 1000
    logger.info(
        "TURN LATENCY call=%s LLM COMPLETE: %.0f ms TTS FIRST AUDIO: %.0f ms "
        "TOTAL TIME TO FIRST AUDIO: %.0f ms legacy_twiml=true",
        CallSid,
        total_ms,
        total_ms,
        total_ms,
    )
    reply = _authoritative_reply(session, reply)
    reply = _authoritative_availability_reply(session, reply)
    reply = _with_gratuity_reminder(session, reply, result)
    session.add_turn("assistant", reply)
    # If the agent just recited the details and asked for the go-ahead, the
    # caller's next "yes" is consent to those exact details — and only then.
    if _asks_for_confirmation(reply) and not guard_statement:
        mark_read_back(session)
    await state.save(session)

    if _is_closing_reply(reply):
        vr = VoiceResponse()
        vr.say(reply, voice=session.voice)
        # Give the carrier a beat to deliver the tail of the TTS audio before
        # tearing the leg down, so the sign-off isn't clipped mid-word.
        vr.pause(length=1)
        vr.hangup()
        await finalize_gather_call(db, state, CallSid)
        return _twiml(vr)

    return _twiml(_gather_twiml(reply, session.voice))


# --------------------------------------------------------------------------- #
# Outbound calls — 6DM Sales Agent only.
#
# This is the dial trigger for the outbound B2B product, so it is fenced to
# super_admin: a spa_admin or spa_staff token gets 403 here, and their bookings
# can never reach Dominic's calendar.
# --------------------------------------------------------------------------- #
@router.post("/voice/outbound", response_model=OutboundCallResponse)
async def voice_outbound(
    payload: OutboundCallRequest,
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> OutboundCallResponse:
    configured_from_number = current_user.twilio_phone_number or settings.TWILIO_PHONE_NUMBER
    from_number = payload.from_number or configured_from_number
    voice = _resolve_voice(None, current_user)

    if not configured_from_number:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No caller ID configured. Set TWILIO_PHONE_NUMBER or the user's twilio_phone_number.",
        )
    if payload.from_number and payload.from_number != configured_from_number:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="from_number must match the configured workspace Twilio number.",
        )

    sales_scope = TenantScope.for_sales_workspace(current_user.id)
    if payload.contact_id:
        # Only dial leads from the sales workspace — never a spa's guest list.
        lead = (
            await db.execute(
                select(Contact).where(
                    scope_filter(sales_scope, Contact),
                    Contact.id == payload.contact_id,
                )
            )
        ).scalar_one_or_none()
        if lead is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "Lead not found in the sales workspace"
            )

    try:
        call_sid = await twilio_service.create_outbound_call(payload.to_number, from_number)
    except TwilioException as exc:
        # Upstream provider rejected the call (bad number, unverified trial number,
        # bad credentials...). Surface it as a 502 rather than an opaque 500.
        logger.error("Twilio rejected outbound call to %s: %s", payload.to_number, exc)
        # TwilioRestException.__str__ embeds ANSI colour codes meant for a terminal;
        # `msg` is the bare provider message.
        reason = getattr(exc, "msg", None) or str(exc)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail=f"Telephony provider rejected the call: {reason}",
        ) from exc

    call_log = CallLog(
        twilio_call_sid=call_sid,
        direction=CallDirection.OUTBOUND,
        status=CallStatus.QUEUED,
        from_number=from_number,
        to_number=payload.to_number,
        contact_id=payload.contact_id,
        user_id=current_user.id,
        tenant_id=None,  # outbound sales is the platform workspace, not a spa
    )
    db.add(call_log)
    await db.commit()
    await db.refresh(call_log)

    session = CallSession(
        call_sid=call_sid,
        direction="outbound",
        from_number=from_number,
        to_number=payload.to_number,
        call_objective=payload.call_objective,
        user_id=str(current_user.id),
        voice=voice,
        business_name="6DM",
        timezone=settings.SALES_TIMEZONE,
        entities={"voice_engine": "xai_realtime" if settings.XAI_REALTIME_ENABLED else "twilio_tts"},
    )
    await state.create(session)

    return OutboundCallResponse(call_sid=call_sid, call_log_id=call_log.id, status="queued")


@router.post("/voice/outbound/answer", dependencies=[Depends(verify_twilio_signature)])
async def voice_outbound_answer(
    CallSid: str = Form(...),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    """Twilio fetches TwiML here when the callee answers the outbound call."""
    session = await state.get(CallSid)
    if settings.XAI_REALTIME_ENABLED and session is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Outbound call session is not ready; retry the answer webhook.")
    if session and session.entities.get("voice_engine", "xai_realtime" if settings.XAI_REALTIME_ENABLED else "twilio_tts") == "xai_realtime":
        if not session.history:
            session.add_turn("assistant", "Hi, this is Cara, the AI assistant with 6DM. Is now a good time to talk?")
            await state.save(session)
        logger.info("Outbound call %s using xAI realtime bridge voice=%s", CallSid, settings.XAI_VOICE_ID)
        return _twiml(_media_stream_twiml(CallSid))
    voice = session.voice if session else settings.DEFAULT_TWIML_VOICE

    if session and session.call_objective:
        session.add_turn(
            "user",
            "[SYSTEM: The callee just answered the phone. Deliver your opening line.]",
        )
        opening = await grok_service.generate_voice_response(session)
        session.history.pop()  # remove synthetic instruction turn
    else:
        opening = "Hello! This is the AI assistant calling. Is now a good time to talk?"

    if session:
        session.add_turn("assistant", opening)
        await state.save(session)

    return _twiml(_gather_twiml(opening, voice))


# --------------------------------------------------------------------------- #
# Lifecycle callbacks
# --------------------------------------------------------------------------- #
@router.post("/voice/status", dependencies=[Depends(verify_twilio_signature)])
async def voice_status(
    CallSid: str = Form(...),
    CallStatus_: str = Form(..., alias="CallStatus"),
    CallDuration: int | None = Form(None),
    db: AsyncSession = Depends(get_db),
    state: CallStateStore = Depends(get_call_state_store),
) -> Response:
    call_log = (
        await db.execute(select(CallLog).where(CallLog.twilio_call_sid == CallSid))
    ).scalar_one_or_none()

    if call_log:
        if CallStatus_ in ("completed", "failed", "busy", "no-answer", "canceled"):
            # Usually a no-op: voice_respond already finalized this call the
            # instant the app itself hung up. This exists for the calls that
            # don't go through that path — the caller hangs up mid-<Gather>
            # with no further request from Twilio, or a callback lands after a
            # crash before voice_respond could finalize.
            await finalize_gather_call(
                db,
                state,
                CallSid,
                status=_TWILIO_STATUS_MAP.get(CallStatus_, CallStatus.COMPLETED),
                duration_seconds=CallDuration,
            )
        else:
            call_log.status = _TWILIO_STATUS_MAP.get(CallStatus_, call_log.status)
            if CallStatus_ in ("in-progress", "answered") and not call_log.started_at:
                call_log.started_at = datetime.now(timezone.utc)
            await db.commit()

    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# Call-history reconciliation
#
# Calls arriving over the SIP trunk never reach the webhooks above, so they are
# absent from `call_logs` and therefore from the dashboard. These let an admin
# pull the account's own call records in, and repair rows that were written with
# no owner and are consequently invisible to every user.
# --------------------------------------------------------------------------- #
@router.post("/sync/twilio")
async def sync_twilio_calls(
    limit: int = Query(50, ge=1, le=1000),
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    report = await sync_calls(db, limit=limit)
    return report.as_dict()


@router.post("/sync/repair-scoping")
async def repair_call_log_scoping(
    current_user: User = Depends(get_super_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await repair_unscoped_call_logs(db)


@router.post("/voice/pay", dependencies=[Depends(verify_twilio_signature)])
async def voice_pay() -> Response:
    """Twilio Pay entry. Used only when a Pay connector is configured.

    The AI media stream does not call this. Card digits stay inside Twilio Pay.
    """
    from xml.sax.saxutils import escape

    from app.services.secure_payment import pay_twiml

    action = f"{settings.PUBLIC_BASE_URL}{settings.API_V1_PREFIX}/telephony/voice/pay/result"
    try:
        xml = pay_twiml(action)
    except ValueError:
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Say>"
            + escape("Payment cannot be collected on this call yet.")
            + "</Say></Response>"
        )
    return Response(content=xml, media_type="application/xml")


@router.post("/voice/pay/result", dependencies=[Depends(verify_twilio_signature)])
async def voice_pay_result(request: Request) -> Response:
    """Store nothing. Return the caller to the receptionist with a safe result only."""
    from app.services.secure_payment import spoken_payment_line, twilio_pay_callback

    form = dict(await request.form())
    try:
        safe = twilio_pay_callback({str(key): str(value) for key, value in form.items()})
        spoken = spoken_payment_line(safe)
    except ValueError:
        spoken = "The payment could not be completed."
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Say>'
        + spoken
        + "</Say></Response>"
    )
    return Response(content=xml, media_type="application/xml")


@router.post("/voice/recording", dependencies=[Depends(verify_twilio_signature)])
async def voice_recording(
    CallSid: str = Form(...),
    RecordingUrl: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    call_log = (
        await db.execute(select(CallLog).where(CallLog.twilio_call_sid == CallSid))
    ).scalar_one_or_none()
    if call_log:
        call_log.recording_url = f"{RecordingUrl}.mp3"
        await db.commit()
    return Response(status_code=204)
