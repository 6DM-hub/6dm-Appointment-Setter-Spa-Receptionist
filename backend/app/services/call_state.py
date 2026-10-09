"""
Redis-backed session memory for active calls.
"""
import json
import logging
import time
import uuid
from typing import Any, Literal

from redis.asyncio import Redis

from app.core.config import settings
from app.core.tenancy import TenantScope

from app.services.truth_log import truth
from app.services.secure_payment import redact_payment_text
from app.services.sensitive_information import sensitive_health_request

logger = logging.getLogger(__name__)

CALL_STATE_PREFIX = "call:state:"
ACTIVE_CALLS_SET = "call:active"

Role = Literal["system", "user", "assistant"]

CALL_PHASE_NEW = "NEW_CALL"
CALL_PHASE_GREETING_REQUESTED = "GREETING_REQUESTED"
CALL_PHASE_GREETING_AUDIO_STARTED = "GREETING_AUDIO_STARTED"
CALL_PHASE_GREETED = "GREETED"
CALL_PHASE_ACTIVE = "ACTIVE_CONVERSATION"
CALL_PHASE_ENDED = "ENDED"
_LEGACY_NEW_PHASES = {CALL_PHASE_NEW, "greeting", ""}
_PHASE_RANK = {
    CALL_PHASE_NEW: 0,
    CALL_PHASE_GREETING_REQUESTED: 1,
    CALL_PHASE_GREETING_AUDIO_STARTED: 2,
    CALL_PHASE_GREETED: 3,
    CALL_PHASE_ACTIVE: 4,
    CALL_PHASE_ENDED: 5,
}


class CallSession:
    def __init__(
        self,
        call_sid: str,
        direction: str,
        from_number: str,
        to_number: str,
        history: list[dict[str, str]] | None = None,
        entities: dict[str, Any] | None = None,
        phase: str = CALL_PHASE_NEW,
        greeting_requested: bool = False,
        greeting_audio_started: bool = False,
        greeting_sent: bool = False,
        call_objective: str | None = None,
        user_id: str | None = None,
        tenant_id: str | None = None,
        business_name: str | None = None,
        tenant_prompt: str | None = None,
        timezone: str | None = None,
        voice: str | None = None,
        booking_status: str = "none",
        selected_service: str | None = None,
        selected_duration: int | None = None,
        requested_datetime: str | None = None,
        confirmed_datetime: str | None = None,
        appointment_id: str | None = None,
        external_booking_id: str | None = None,
        created_at: float | None = None,
    ) -> None:
        self.call_sid = call_sid
        self.direction = direction
        self.from_number = from_number
        self.to_number = to_number
        self.history = [dict(turn, content=redact_payment_text(turn.get("content", ""))) for turn in (history or [])]
        self.entities = entities or {}
        self.phase = phase or CALL_PHASE_NEW
        self.greeting_requested = bool(greeting_requested)
        self.greeting_audio_started = bool(greeting_audio_started)
        self.greeting_sent = bool(greeting_sent)
        self.call_objective = call_objective
        self.user_id = user_id  # 6DM sales workspace owner, for outbound calls
        # Spa tenant resolved from the dialed number on inbound calls. Exactly
        # one of user_id / tenant_id identifies the scope every record this call
        # creates is written into.
        self.tenant_id = tenant_id
        self.business_name = business_name
        # Tenant-specific receptionist persona, rendered once at call setup from
        # SpaAccount (prompt + services + staff + hours) so every turn is
        # answered with the same configuration even if the row changes mid-call.
        self.tenant_prompt = tenant_prompt
        # IANA timezone of the spa answering this call (e.g. "America/Chicago").
        # None for outbound sales calls or an unresolved tenant, in which case
        # datetime resolution falls back to UTC rather than guessing a locale.
        self.timezone = timezone
        self.voice = voice or settings.DEFAULT_TWIML_VOICE  # Twilio <Say> voice for this call
        self.booking_status = booking_status
        self.selected_service = selected_service
        self.selected_duration = selected_duration
        self.requested_datetime = requested_datetime
        self.confirmed_datetime = confirmed_datetime
        self.appointment_id = appointment_id
        self.external_booking_id = external_booking_id
        self.created_at = created_at or time.time()

    def add_turn(self, role: Role, content: str) -> None:
        if role == "user" and sensitive_health_request(content):
            self.entities["sensitive_health_request"] = True
        self.history.append({"role": role, "content": redact_payment_text(content)})

    @property
    def scope(self) -> TenantScope | None:
        """Where records created during this call belong.

        None means the call could not be attributed to a spa or a workspace —
        an inbound call to an unclaimed number — in which case nothing is
        persisted rather than being written somewhere arbitrary.
        """
        if self.tenant_id:
            return TenantScope.for_tenant(uuid.UUID(self.tenant_id))
        if self.user_id:
            return TenantScope.for_sales_workspace(uuid.UUID(self.user_id))
        return None

    @property
    def customer_phone(self) -> str:
        """The human caller/callee's number, regardless of call direction."""
        return self.from_number if self.direction == "inbound" else self.to_number

    def to_dict(self) -> dict[str, Any]:
        return {
            "call_sid": self.call_sid,
            "direction": self.direction,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "history": [dict(turn, content=redact_payment_text(turn.get("content", ""))) for turn in self.history],
            "entities": self.entities,
            "phase": self.phase,
            "greeting_requested": self.greeting_requested,
            "greeting_audio_started": self.greeting_audio_started,
            "greeting_sent": self.greeting_sent,
            "call_objective": self.call_objective,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "business_name": self.business_name,
            "tenant_prompt": self.tenant_prompt,
            "timezone": self.timezone,
            "voice": self.voice,
            "booking_status": self.booking_status,
            "selected_service": self.selected_service,
            "selected_duration": self.selected_duration,
            "requested_datetime": self.requested_datetime,
            "confirmed_datetime": self.confirmed_datetime,
            "appointment_id": self.appointment_id,
            "external_booking_id": self.external_booking_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallSession":
        # Tolerate payloads written by an older release still sitting in Redis:
        # unknown keys are dropped rather than blowing up a live call.
        known = cls.__init__.__code__.co_varnames[1 : cls.__init__.__code__.co_argcount]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def transcript_text(self) -> str:
        lines = []
        for turn in self.history:
            if turn["role"] == "system":
                continue
            speaker = "Agent" if turn["role"] == "assistant" else "Caller"
            lines.append(f"{speaker}: {redact_payment_text(turn['content'])}")
        return "\n".join(lines)


def _normalize_call_phase(phase: str | None) -> str:
    if phase in _LEGACY_NEW_PHASES or phase is None:
        return CALL_PHASE_NEW
    return phase


def set_call_phase(session: CallSession, new_phase: str) -> None:
    """Advance call conversational phase. Never moves backward."""
    old = _normalize_call_phase(getattr(session, "phase", None))
    new_phase = _normalize_call_phase(new_phase)
    if old == new_phase:
        return
    if _PHASE_RANK.get(new_phase, 0) < _PHASE_RANK.get(old, 0):
        return
    session.phase = new_phase
    truth("CALL_PHASE_CHANGED", call_sid=session.call_sid, old=old, new=new_phase)


def mark_greeting_requested(session: CallSession, *, method: str = "force_message") -> bool:
    """Record that the opening greeting was asked of xAI. Not yet heard."""
    if session.greeting_sent:
        truth("CALL_GREETING_SUPPRESSED", call_sid=session.call_sid, reason="already_greeted")
        return False
    if session.greeting_requested:
        return True
    session.greeting_requested = True
    set_call_phase(session, CALL_PHASE_GREETING_REQUESTED)
    truth("CALL_GREETING_REQUESTED", call_sid=session.call_sid, method=method)
    return True


def mark_greeting_sent(session: CallSession, *, response_id: str | None = None) -> bool:
    """Record that opening greeting audio entered the outbound path."""
    if session.greeting_sent:
        return False
    session.greeting_requested = True
    session.greeting_audio_started = True
    session.greeting_sent = True
    set_call_phase(session, CALL_PHASE_GREETING_AUDIO_STARTED)
    set_call_phase(session, CALL_PHASE_GREETED)
    truth(
        "CALL_GREETING_AUDIO_STARTED",
        call_sid=session.call_sid,
        response_id=response_id,
    )
    truth("CALL_GREETING_SENT", call_sid=session.call_sid, response_id=response_id)
    return True


def mark_conversation_active(session: CallSession) -> None:
    if _normalize_call_phase(session.phase) == CALL_PHASE_ENDED:
        return
    if not session.greeting_sent:
        return
    set_call_phase(session, CALL_PHASE_ACTIVE)


def mark_call_ended(session: CallSession) -> None:
    set_call_phase(session, CALL_PHASE_ENDED)


class CallStateStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._ttl = settings.CALL_STATE_TTL_SECONDS

    def _key(self, call_sid: str) -> str:
        return f"{CALL_STATE_PREFIX}{call_sid}"

    async def create(self, session: CallSession) -> CallSession:
        await self.save(session)
        await self._redis.sadd(ACTIVE_CALLS_SET, session.call_sid)
        logger.info("Call session created: %s", session.call_sid)
        return session

    async def get(self, call_sid: str) -> CallSession | None:
        raw = await self._redis.get(self._key(call_sid))
        if raw is None:
            return None
        return CallSession.from_dict(json.loads(raw))

    async def save(self, session: CallSession) -> None:
        await self._redis.set(
            self._key(session.call_sid), json.dumps(session.to_dict()), ex=self._ttl
        )

    async def append_turn(self, call_sid: str, role: Role, content: str) -> CallSession | None:
        session = await self.get(call_sid)
        if session is None:
            return None
        session.add_turn(role, content)
        await self.save(session)
        return session

    async def set_entities(self, call_sid: str, entities: dict[str, Any]) -> None:
        session = await self.get(call_sid)
        if session is None:
            return
        session.entities.update(entities)
        await self.save(session)

    async def end(self, call_sid: str) -> CallSession | None:
        session = await self.get(call_sid)
        await self._redis.srem(ACTIVE_CALLS_SET, call_sid)
        if session:
            await self._redis.expire(self._key(call_sid), 300)
        logger.info("Call session ended: %s", call_sid)
        return session

    async def active_call_sids(self) -> set[str]:
        members = await self._redis.smembers(ACTIVE_CALLS_SET)
        return {m.decode() if isinstance(m, bytes) else m for m in members}
