"""Optional TLS-only SMTP transport. Credentials stay in server settings."""
import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from app.core.config import settings


async def send_email(destination, summary):
    if not settings.STAFF_SMTP_HOST or not settings.STAFF_SMTP_FROM:
        return "not_configured"
    def send():
        message = EmailMessage()
        message["From"] = settings.STAFF_SMTP_FROM
        message["To"] = destination
        message["Subject"] = "Cara: Needs Staff Attention"
        message.set_content(summary)
        with smtplib.SMTP_SSL(settings.STAFF_SMTP_HOST, settings.STAFF_SMTP_PORT,
                              timeout=10, context=ssl.create_default_context()) as client:
            if settings.STAFF_SMTP_USER:
                client.login(settings.STAFF_SMTP_USER, settings.STAFF_SMTP_PASSWORD)
            refused = client.send_message(message)
            return "rejected" if refused else "accepted"
    return await asyncio.to_thread(send)


async def send_staff(spa, event, summary):
    from app.services.twilio_service import twilio_service
    config = spa.notification_settings or {}
    rules = config.get("booking_escalation") or config
    deliveries = []
    for channel, destinations in (("sms", rules.get("sms_destinations", [])), ("email", rules.get("email_destinations", []))):
        for destination in dict.fromkeys(destinations):
            try:
                if channel == "sms":
                    result = "accepted" if await twilio_service.send_sms(destination, summary) else "failed_or_unknown"
                else:
                    result = await send_email(destination, summary)
            except Exception:
                result = "delivery_unknown"
            deliveries.append({"channel": channel, "destination": destination, "status": result})
    return {"status": "attempted" if deliveries else "no_destination", "deliveries": deliveries}
