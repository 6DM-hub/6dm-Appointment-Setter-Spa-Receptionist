"""Twilio REST client wrapper for outbound calls, executed off the event loop."""

import asyncio
import logging
from functools import partial

from twilio.rest import Client

from app.core.config import settings

logger = logging.getLogger(__name__)


class TwilioService:
    def __init__(self) -> None:
        self._client = Client(
            settings.TWILIO_ACCOUNT_SID,
            settings.TWILIO_AUTH_TOKEN,
        )

    async def create_outbound_call(self, to_number: str, from_number: str | None = None) -> str:
        """
        Place an outbound call.

        `from_number` lets a multi-tenant caller override the workspace default
        (User.twilio_phone_number); it falls back to the global configured number.
        """
        base = f"{settings.PUBLIC_BASE_URL}{settings.API_V1_PREFIX}/telephony"

        kwargs: dict[str, object] = {
            "to": to_number,
            "from_": from_number or settings.TWILIO_PHONE_NUMBER,
            "url": f"{base}/voice/outbound/answer",
            "method": "POST",
            "status_callback": f"{base}/voice/status",
            "status_callback_method": "POST",
            "status_callback_event": [
                "initiated",
                "ringing",
                "answered",
                "completed",
            ],
        }

        # Trial accounts reject these outright, failing the whole call request.
        if settings.TWILIO_ENABLE_RECORDING:
            kwargs |= {
                "record": True,
                "recording_status_callback": f"{base}/voice/recording",
                "recording_status_callback_method": "POST",
            }

        loop = asyncio.get_running_loop()

        call = await loop.run_in_executor(
            None,
            partial(self._client.calls.create, **kwargs),
        )

        logger.info(
            "Outbound call created: %s -> %s",
            call.sid,
            to_number,
        )

        return call.sid

    async def send_sms(self, to_number: str, body: str, from_number: str | None = None) -> str | None:
        """Send a staff SMS. Returns None when no from-number or account is configured."""
        sender = (from_number or settings.TWILIO_PHONE_NUMBER or "").strip()
        if not sender or not (to_number or "").strip() or not settings.TWILIO_ACCOUNT_SID:
            logger.info("SMS skipped: Twilio sender or account is not configured")
            return None
        loop = asyncio.get_running_loop()
        message = await loop.run_in_executor(
            None,
            partial(
                self._client.messages.create,
                to=to_number,
                from_=sender,
                body=body,
            ),
        )
        return getattr(message, "sid", None)

    async def set_inbound_status_callback(self, call_sid: str) -> None:
        """Attach a status callback to an inbound call already in progress.

        Twilio has no TwiML verb for this — inbound calls only get a
        `StatusCallback` if the receiving number is configured with one in the
        console, which this deployment doesn't rely on. Updating the live Call
        resource here is what lets `voice_status` fire on completion and flush
        `session.transcript_text` into `call_logs.transcript` for inbound calls.
        """
        base = f"{settings.PUBLIC_BASE_URL}{settings.API_V1_PREFIX}/telephony"
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                partial(
                    self._client.calls(call_sid).update,
                    status_callback=f"{base}/voice/status",
                    status_callback_method="POST",
                ),
            )
        except Exception:
            logger.warning(
                "Failed to attach status callback to inbound call %s", call_sid, exc_info=True
            )

    def list_recent_calls(self, limit: int = 50) -> list:
        """Most recent calls on the account, newest first.

        Synchronous on purpose: the Twilio SDK blocks, so callers run this in an
        executor (see app/services/twilio_sync.py).
        """
        return list(self._client.calls.list(limit=limit))


# IMPORTANT: telephony.py imports this exact name
twilio_service = TwilioService()