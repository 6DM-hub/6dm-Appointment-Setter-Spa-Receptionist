"""Opt-in deployment checks and idempotent phone routing configuration.

Runs inside the backend so provider secrets never leave Railway. No phone call
is placed: the two audio probes are private synthetic realtime sessions.
"""
import asyncio
import base64
import json
import logging
from urllib.parse import urlsplit

import httpx
from redis.asyncio import Redis
from sqlalchemy import select
from websockets.asyncio.client import connect

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import SpaAccount, VoiceEngine
from app.services.call_state import CallSession
from app.services.media_bridge import TwilioMediaBridge
from app.services.twilio_service import twilio_service
from app.services.voice_config import resolve_xai_voice
from app.services.xai_realtime import build_xai_realtime_url, _AUDIO_DELTA_EVENTS

logger = logging.getLogger(__name__)


def _voice_ids(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {"id", "voice_id"} and isinstance(item, str):
                yield item.lower()
            else:
                yield from _voice_ids(item)
    elif isinstance(value, list):
        for item in value:
            yield from _voice_ids(item)


async def probe_audio(direction: str) -> dict:
    """Use the actual bridge session configuration and verify generated PCMU."""
    session = CallSession(
        call_sid=f"voice-check-{direction}", direction=direction,
        from_number="", to_number="", business_name="6DM",
        timezone=settings.SALES_TIMEZONE,
        call_objective="Only confirm the connection check; do not use tools.",
    )
    bridge = TwilioMediaBridge(session.call_sid, session, None)
    async with connect(
        build_xai_realtime_url(), open_timeout=15,
        additional_headers={"Authorization": f"Bearer {settings.XAI_API_KEY}"},
    ) as ws:
        bridge._ws = ws
        await bridge._configure()
        configured = False
        audio_bytes = 0
        async with asyncio.timeout(30):
            while True:
                event = json.loads(await ws.recv())
                kind = event.get("type")
                if kind == "error" or (not kind and "error" in event):
                    error = event.get("error") or {}
                    code = error.get("code", "provider_error") if isinstance(error, dict) else "provider_error"
                    raise RuntimeError(f"xAI session rejected configuration ({code})")
                if kind == "session.updated" and not configured:
                    configured = True
                    await ws.send(json.dumps({
                        "type": "conversation.item.create",
                        "item": {"type": "message", "role": "user", "content": [
                            {"type": "input_text", "text": "Say only: Voice connection ready."},
                        ]},
                    }))
                    await ws.send(json.dumps({"type": "response.create"}))
                if kind in _AUDIO_DELTA_EVENTS:
                    audio_bytes += len(base64.b64decode(event.get("delta") or "", validate=True))
                if kind in {"response.done", "response.completed"}:
                    if not configured or audio_bytes == 0:
                        raise RuntimeError("xAI returned no audio")
                    break
        return {"status": "ready", "encoding": "audio/pcmu", "sample_rate": 8000, "audio_bytes": audio_bytes}


def configure_twilio_numbers(numbers: list[str]) -> dict:
    client = twilio_service._client
    client.api.accounts(settings.TWILIO_ACCOUNT_SID).fetch()
    base = settings.PUBLIC_BASE_URL.rstrip("/") + settings.API_V1_PREFIX + "/telephony"
    configured = 0
    missing = 0
    for number in sorted(set(numbers)):
        matches = client.incoming_phone_numbers.list(phone_number=number, limit=2)
        if len(matches) != 1:
            missing += 1
            continue
        phone = matches[0]
        updated = client.incoming_phone_numbers(phone.sid).update(
            voice_url=base + "/voice/inbound", voice_method="POST",
            status_callback=base + "/voice/status", status_callback_method="POST",
            trunk_sid="", voice_application_sid="",
        )
        if updated.voice_url != base + "/voice/inbound" or updated.trunk_sid or updated.voice_application_sid:
            raise RuntimeError("Twilio inbound webhook did not take effect")
        configured += 1
    outbound = bool(settings.TWILIO_PHONE_NUMBER and client.incoming_phone_numbers.list(
        phone_number=settings.TWILIO_PHONE_NUMBER, limit=1,
    ))
    return {"configured_numbers": configured, "unmatched_numbers": missing, "outbound_caller_id": outbound}


async def setup_voice() -> dict:
    result = {"release": settings.VOICE_RELEASE, "voice": resolve_xai_voice(settings.XAI_VOICE_ID), "ready": False}
    stage = "settings"
    try:
        if not settings.XAI_API_KEY:
            raise RuntimeError("XAI_API_KEY is not configured")
        if not settings.XAI_REALTIME_ENABLED:
            raise RuntimeError("XAI_REALTIME_ENABLED must be true for outbound")
        if urlsplit(settings.PUBLIC_BASE_URL).scheme != "https":
            raise RuntimeError("PUBLIC_BASE_URL must use HTTPS")
        if result["voice"] != "ara":
            raise RuntimeError("XAI_VOICE_ID must be ara for this deployment")
        stage = "voice_catalogue"
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(settings.XAI_BASE_URL.rstrip("/") + "/tts/voices",
                headers={"Authorization": f"Bearer {settings.XAI_API_KEY}"})
            if response.status_code == 200:
                if "ara" not in set(_voice_ids(response.json())):
                    raise RuntimeError("ara is absent from the available voice catalogue")
                result["catalogue"] = "ara_verified"
            elif response.status_code in {403, 404}:
                result["catalogue"] = "unavailable; verifying documented ara through realtime audio"
            else:
                raise RuntimeError(f"xAI voice catalogue HTTP {response.status_code}")
        for direction in ("inbound", "outbound"):
            stage = f"{direction}_audio"
            result[direction] = await probe_audio(direction)
        stage = "redis"
        redis = Redis.from_url(settings.REDIS_URL)
        try:
            await redis.ping()
        finally:
            await redis.aclose()
        result["redis"] = "connected"
        stage = "spa_and_twilio_configuration"
        async with AsyncSessionLocal() as db:
            spas = list((await db.execute(select(SpaAccount).where(SpaAccount.is_active.is_(True)))).scalars())
            numbers = [spa.twilio_phone_number for spa in spas if spa.twilio_phone_number]
            result["twilio"] = await asyncio.to_thread(configure_twilio_numbers, numbers)
            for spa in spas:
                spa.voice_engine = VoiceEngine.XAI_REALTIME
            await db.commit()
            result["active_spas"] = len(spas)
            result["spas_without_numbers"] = len(spas) - len(numbers)
        result["ready"] = bool(
            result["active_spas"] and result["twilio"]["configured_numbers"]
            and not result["twilio"]["unmatched_numbers"]
            and result["twilio"]["outbound_caller_id"]
        )
        logger.info("VOICE_SETUP %s", json.dumps(result))
    except Exception as exc:
        # Provider exceptions may embed request details; never log their bodies.
        result["failed_stage"] = stage
        result["error_type"] = type(exc).__name__
        result["provider_status"] = getattr(exc, "status", None)
        logger.error("VOICE_SETUP_FAILED stage=%s error_type=%s provider_status=%s", stage, type(exc).__name__, result["provider_status"])
    return result
