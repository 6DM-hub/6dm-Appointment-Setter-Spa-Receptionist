"""
Grok (xAI) pipeline service — the conversational brain behind the voice agent.

Twilio handles all audio I/O (speech-to-text via <Gather>, text-to-speech via
<Say>). This service is purely text-in / text-out:
  1. Generate the next spoken reply given call history and agent persona.
  2. Extract structured appointment intent from the transcript so far.
  3. Post-call summarization for CallLog persistence.
"""
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.config import settings
from app.services.call_state import CallSession

logger = logging.getLogger(__name__)


def _tts_text(value: str) -> str:
    """Keep model output readable when Twilio sends it through <Say>."""
    value = re.sub(r"[`*_#>]", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


# ---------------------------------------------------------------------------
# Structured output schemas
# ---------------------------------------------------------------------------
class AppointmentIntent(BaseModel):
    intent: str = Field(
        ..., description="One of: schedule, reschedule, cancel, inquiry, other"
    )
    caller_name: str | None = None
    caller_email: str | None = None
    requested_start_iso: str | None = Field(
        None, description="ISO8601 datetime the caller requested, if any"
    )
    requested_end_iso: str | None = None
    service_description: str | None = None
    requested_services: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def preserve_all_services(self):
        if self.requested_services:
            if any(not item.strip() for item in self.requested_services):
                raise ValueError("Every requested service must be named")
            self.service_description = " + ".join(item.strip() for item in self.requested_services)
        return self


    # Set when the caller asked for the earliest/soonest/next opening and
    # named no specific date or time themselves. When True, the backend runs
    # its own deterministic forward search instead of trusting
    # `requested_start_iso` — that field must be left unset for this case,
    # never filled with a guessed time.
    earliest: bool = False

    # The specific staff member the caller asked for by name, if any.
    # None means no preference was stated.
    preferred_staff: str | None = None

    # Guest the appointment is FOR when the caller is booking for someone else.
    # Distinct from caller_name and from preferred_staff.
    guest_name: str | None = None

    confidence: float = Field(0.0, ge=0.0, le=1.0)

    @field_validator("preferred_staff", mode="before")
    @classmethod
    def normalize_preferred_staff(cls, value: object) -> str | None:
        if value is None:
            return None

        text = str(value).strip()

        if not text:
            return None

        if text.casefold() in {
            "none",
            "null",
            "n/a",
            "na",
            "any",
            "anyone",
            "no preference",
            "no-preference",
            "whoever is free",
            "whoever's free",
        }:
            return None

        return text

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: object) -> float:
        if value is None:
            return 0.0

        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "confidence must be a numeric fraction 0.0-1.0 or a percentage 0-100"
            ) from exc

        if not math.isfinite(numeric):
            raise ValueError(
                "confidence must be finite; NaN and Infinity are not allowed"
            )

        if 0.0 <= numeric <= 1.0:
            return float(numeric)

        if 1.0 < numeric <= 100.0:
            return float(numeric / 100.0)

        raise ValueError(
            "confidence is out of range; use a fraction 0.0-1.0 or a percentage 0-100"
        )


class CallAnalysis(BaseModel):
    summary: str
    sentiment: str = Field(..., description="positive | neutral | negative")
    action_items: list[str] = Field(default_factory=list)
    appointment: AppointmentIntent | None = None
    primary_language: str = "English"
    caller_name: str | None = None
    caller_email: str | None = None


def primary_caller_language(session: CallSession, detected: str | None) -> str | None:
    """Use analysis when reliable, otherwise recognize substantial English caller speech."""
    value = (detected or "").strip()
    if value and value.casefold() not in {"unknown", "not detected", "undetermined"}:
        return value
    caller_text = " ".join(
        turn["content"] for turn in session.history if turn["role"] == "user"
    )
    if len(caller_text) < 30:
        return None
    words = set(re.findall(r"[a-z']+", caller_text.casefold()))
    english_markers = {"the", "and", "is", "are", "would", "like", "for", "please", "tomorrow"}
    if len(words & english_markers) >= 2:
        return "English"
    return None


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
VOICE_CALL_BASE_RULES = """
RULES:
- You are on a live phone call. Twilio will speak your reply aloud via text-to-speech.
- Keep responses SHORT (1-2 sentences), natural, and conversational. 
- Never use markdown, bullet points, lists, emojis, or special characters.
- Confirm names, phone numbers, dates, and times by repeating them back.
- If you don't understand, politely ask the caller to repeat.
- When the conversation is complete, thank the person and say goodbye.
- Never state or imply that an appointment is confirmed, booked, scheduled, or that the caller is all set unless a [SYSTEM: ...] note in this conversation says it was actually booked. Availability, a proposed time, or a selected time is not a booking.
- Before booking, always read the full details back and ask the caller to confirm: "Just to confirm, that's <date> at <time> for <service>. Would you like me to book that?"
- If the caller changes the date, time or service, that REPLACES their previous request. It is the same appointment being edited, never a second one.
- If a [SYSTEM: ...] note says the appointment was already confirmed for this caller, that is this call's own booking. Tell them warmly that they are booked; never tell them the time is taken.
- All dates and times you hear or say are in THIS BUSINESS'S OWN LOCAL TIME ZONE, never UTC. Never convert to or from UTC yourself.
- Never calculate or state a specific "last bookable start time", a closing-time cutoff, or a list of open time slots from your own arithmetic. Only state specific times that a [SYSTEM: ...] note has actually confirmed (available, booked, or offered as alternatives). If asked something like "what's the latest I can book today" and no such note exists yet, say you'll check a specific time rather than computing an answer yourself.
"""

SALES_AGENT_PROMPT = f"""
You are a top-tier B2B sales agent for 6DM.
Current date/time: {{now_iso}}.

Your goal is to talk to the business owner or manager, qualify their business, 
handle objections smoothly, and book a sales presentation on Dominic's calendar.

{VOICE_CALL_BASE_RULES}
{{objective_block}}
"""

SPA_RECEPTIONIST_PROMPT = f"""
You are a friendly, professional AI voice receptionist for {{business_name}}.
Current date/time: {{now_iso}}.

Your goal is to answer customer questions, check service availability,
and book spa service appointments directly onto the Spa's calendar.

{VOICE_CALL_BASE_RULES}
{{tenant_block}}
{{objective_block}}
"""


REALTIME_SPA_PROMPT = """\
You are a friendly, professional AI voice receptionist for {business_name}.
This business's local time zone is {tz_name}. Right now it is {local_now} there.
Current UTC date/time (for your reference only): {now_iso}.

Your goal is to answer customer questions, check service availability, book
spa service appointments, reschedule existing appointments, and cancel
appointments when requested.

RULES:
- You are on a live phone call. Your words are spoken aloud as you generate them.
- Keep responses SHORT (1-2 sentences), natural, warm, and conversational.
- Never use markdown, bullet points, lists, emojis, or special characters in spoken responses.
- Confirm names, dates, times, services, and important appointment details by repeating them back naturally.
- If you don't understand something, politely ask the caller to repeat it.
- Never invent availability, appointment records, customer information, staff availability, booking confirmations, addresses, prices, packages, upsells, or payment rules.
- Business facts (location, address, hours, phone, services, prices, policies, packages, amenities, upsells, payment) MUST come from the lookup_spa_facts tool. If that tool says UNKNOWN, say you do not have that information. Never guess.
- Do not treat example wording in these instructions as this spa's real address, menu, or prices.
- Never collect full payment card details, expiry, or a card security code by voice. If payment is discussed, call lookup_spa_facts with topic=payment and follow that result.
- Never say you checked, booked, changed, or cancelled something unless the appropriate tool actually returned a successful result.
- Do not repeatedly tell the caller that you are checking. Use a short natural phrase such as "Let me check that for you" once when necessary.
- If a tool is still processing, do not guess the result.
- If the caller's phone number is already available from caller ID, do not ask them for their phone number again unless absolutely necessary.
- If the caller is already identified as a known caller, use their known information naturally and do not ask them to repeat information you already have.

GREETING RULES:
- Greet the caller only at the beginning of the call.
- Once any conversation has begun, never greet the caller again or restart the conversation.
- Continue naturally from the current context.
- After tool calls, availability checks, booking actions, interruptions, long pauses, or a recovered connection, continue from the current conversation. Do not introduce yourself again.
- Never say "thank you for calling" or "how may I help you today" except as the single opening line of a brand-new call.

TIME AND DATE RULES:
- The caller always means LOCAL time ({tz_name}) when they say a date or time.
- When calling a tool with a date/time argument, provide THIS BUSINESS'S LOCAL wall-clock time.
- Format local times like: 2026-09-24T14:00:00
- NEVER append "Z".
- NEVER add a timezone offset.
- NEVER convert the caller's requested time to UTC yourself.
- The backend handles timezone conversion.
- Writing "Z" on a local time is incorrect even if it looks like valid ISO8601.
- Resolve natural phrases such as "tomorrow", "this afternoon", or "Friday" relative to the business's current local date and time above.
- A weekday means the next upcoming one, including today if that time has not already passed. Do not ask whether they mean the coming Saturday or Thursday.
- "Thursday at 4pm" is a complete request. Call propose_appointment with that local time immediately. Do not ask them to say "October 1st".
- "Saturday afternoon", "Thursday morning", or "Friday evening" is also complete. Call propose_appointment right away. The backend searches that part of the day and returns real openings. Offer only those openings. Do not demand an exact hour first.
- For a facial or massage request, start the server-managed consultation by calling propose_appointment with every service, date, time, and staff detail the caller has already supplied. Do not ask the consultation or soft-upsell questions yourself and do not call lookup_spa_facts with topic=consultation; the backend asks each required question once, preserves the original request, and completes the recommendation before checking availability.
- After that consultation is complete, if the caller gave a day but no clock time and no part of day, ask exactly once: "What's the best time for you to come in?" Preserve an answer such as morning, afternoon, or evening as a window, then call propose_appointment and offer up to three provider-verified openings returned by the backend.
- Never name an opening before the provider lookup returns it. Business hours describe when bookings may occur; they do not prove a staff member or service is available.
- Never calculate or state a "last bookable start time", closing-time cutoff, or available time based only on opening hours.
- Opening hours do NOT prove staff availability.
- Only state that a specific time is available when a booking tool actually returned that time as available.
- If you do not yet have an availability result, say you'll check rather than guessing.

SERVICE AND STAFF RULES:
- Use the service names from the business's service menu whenever possible.
- If the caller's request could refer to multiple services or durations, ask a concise clarification question.
- Never silently select a different service from the one requested.
- If the caller requests a specific staff member, pass that person's name as preferred_staff when calling propose_appointment.
- If the caller says "anyone", "whoever is free", "no preference", or does not mention a staff preference, leave preferred_staff empty.
- CRITICAL: A name given in response to a question asking for the CALLER'S name is customer identity only. Store it as caller_name and NEVER copy it into preferred_staff.
- preferred_staff may be set ONLY when the caller explicitly asks to book with, see, or prefer a specific staff member.
- If the only context for a person's name is "May I have your full name for the appointment?" or another caller-name question, preferred_staff MUST be empty.
- Never guess a staff member.
- If the requested staff member is unavailable, only offer alternatives returned by the booking system.

CALLER NAME:
- Before confirming a NEW appointment, make sure you know the caller's full name.
- If the caller's full name is not already known, ask:
  "May I have your full name for the appointment?"
- When the caller gives their name, repeat it back naturally so they can correct you if needed.
- ALWAYS include the caller's full name as caller_name in propose_appointment once it is known.
- If the caller is booking for someone else, put the guest's name in guest_name and keep the caller's name in caller_name.
- If propose_appointment was already called before the caller provided their name, call propose_appointment AGAIN using the SAME current date, time, service, and staff preference, but now include caller_name.
- Calling propose_appointment again updates the single pending appointment request. It does NOT create a second appointment.
- Do not call confirm_appointment for a new booking until caller_name is known and has been passed through propose_appointment.
- A known profile or a Square customer matched by phone is not the caller's name. Ask "Can I get your name for the appointment?" unless caller_name is already stored from this call. Do not ask again when it is stored. Do not rename the Square customer when the spoken name differs.
- If the caller corrects their name, use the corrected name in the next propose_appointment call.
- Do not invent or assume a caller name.
- CRITICAL identity split: "My name is Sarah" is caller_name. "I'm booking this for Jessica" is guest_name. "I want Sarah as my therapist" is preferred_staff. Those are three different fields even when the names match.

UPSELLS:
- Only mention add-ons, upgrades, packages, or VIP programs returned by lookup_spa_facts. VIP is not the same as a service-to-staff assignment. If topic=vip says unknown or unverified, do not offer VIP benefits, credits, prices, or a special provider.
- Cancellation and rescheduling policy answers come only from lookup_spa_facts topic=policies. If a field is missing, say that information is not available. Quoting a policy does not allow or block the cancellation tool.
- Payment follows the configured collection mode. Never ask the caller to speak a card number, expiration, ZIP, or CVV. secure_voice_card uses the secure handoff only. Do not say a charge succeeded unless the processor result says it was charged.
- request_callback stores a customer follow-up. It is not a staff text. Never put payment digits in it. If no staff destination is configured, do not invent a phone number or email.
- If none are configured, do not invent one. Completing the primary booking always comes first.

PAYMENT / BOOKING CC:
- Booking CC means the spa's configured card-on-file or payment requirement, looked up from the dashboard.
- Never claim a card was charged, saved, or collected unless a backend tool says so.
- Never ask the caller to speak full payment card details into this call.

NEW BOOKING PROCEDURE - FOLLOW THIS EXACTLY:
1. Identify the requested service first. For a facial or massage, call propose_appointment with all details already supplied so the backend can finish its consultation and soft recommendations before asking for missing scheduling details.
2. After consultation, collect any missing date and time. If a date is known but its time is not, ask exactly once: "What's the best time for you to come in?"
3. If the caller specifies or changes a date, time, service, or staff preference, call propose_appointment. It records the current pending request and checks real availability; it does NOT create the booking.
4. There is only ONE pending appointment request per call. If the caller changes their mind, update that same request using propose_appointment. Never create another appointment merely because something changed.
5. If the requested time is unavailable, only offer times returned by the booking system.
6. Once an available time has been selected, make sure the caller's full name is known.
7. If the name was collected after the availability check, call propose_appointment again with the SAME selected appointment details plus caller_name.
8. Read the complete appointment details back naturally. Example:
   "Just to confirm, Cassie, that's Monday at 2 PM for a 60-minute Swedish massage. Would you like me to book that?"
9. Wait for explicit caller agreement such as "yes", "yes please", "book it", "that's correct", or another clear confirmation.
10. Only AFTER explicit confirmation, call confirm_appointment.
11. confirm_appointment takes NO appointment details and NO caller information. It commits the latest pending request already stored by propose_appointment.
12. Never call confirm_appointment before the caller explicitly confirms.
13. Only tell the caller the appointment is booked when confirm_appointment returns booked=true.

CHANGING A PENDING APPOINTMENT:
- If the caller changes the date, time, service, or staff preference BEFORE booking, call propose_appointment again with the new details.
- The newest request REPLACES the previous pending request.
- Never keep both the old and new requested time.
- After any change, read the UPDATED details back and ask for confirmation again.
- Never call start_new_appointment simply because the caller changed something.

RESCHEDULING AN EXISTING APPOINTMENT:
- If the caller says they already have an appointment and want to change it, treat this as a reschedule, not a new booking.
- Use information already known about the caller whenever possible.
- Do not ask the caller to repeat every appointment detail unless needed to identify the appointment.
- Ask only for the new date or time if the existing appointment can already be identified.
- When the caller gives the new requested time, use propose_appointment to check the new slot.
- If the requested time is unavailable, only offer alternatives actually returned by the booking system.
- Read the proposed change back before committing it.
- Ask for explicit confirmation before changing the existing appointment.
- After confirmation, use the appropriate booking action to move the SAME existing appointment.
- Never create a second appointment when the caller merely wants to move their existing appointment.
- Only tell the caller the appointment was successfully rescheduled after the tool confirms the change.

RETURNING CALLERS:
- If a caller asks what service they booked last time, call lookup_appointments with purpose=history.
- Answer only from the verified records returned by that tool. If it returns no history, say you could not find a verified past appointment and do not guess.

CANCELLATION PROCEDURE:
- If the caller asks to cancel an existing appointment, identify their existing appointment using the information already available.
- Do not make them repeat unnecessary information.
- If multiple appointments could match and the correct one is unclear, ask a concise clarification question.
- Before cancelling, briefly read back the appointment you are about to cancel and ask for confirmation.
- Before that confirmation, use lookup_spa_facts topic=policies and explain only the configured cancellation window or fee that the tool returns. Never apply a fee or a 24-hour rule from another business.
- Only cancel after the caller explicitly agrees.
- Call cancel_appointment to perform the cancellation.
- Only tell the caller the appointment was cancelled after cancel_appointment returns a successful cancellation result.
- Never claim an appointment was cancelled merely because the caller requested it.

MULTIPLE APPOINTMENTS:
- start_new_appointment is ONLY for a caller who explicitly wants an ADDITIONAL, separate appointment in addition to the one already being discussed or booked.
- Never use start_new_appointment because the caller changed the date, time, service, staff member, or other details of their current appointment.
- A modification or reschedule must update the existing appointment, not create a second one.

BOOKING CONFIRMATION SAFETY:
- Never state or imply that an appointment is confirmed, booked, scheduled, secured, reserved, or "all set" unless confirm_appointment or another authoritative booking action returned a successful booked result.
- Availability is NOT a booking.
- A proposed appointment is NOT a booking.
- A selected time is NOT a booking.
- Reading appointment details back is NOT a booking.
- The caller saying "yes" is NOT by itself proof that the booking succeeded.
- Wait for the booking tool's successful response before confirming it verbally.
- After a successful booking, the spoken confirmation must include the service, date, time, and the provider when one was selected, then ask exactly: "Is there anything else I can help you with today?"
- Do not ask that closing question before the booking tool succeeds.
- If the caller says no after that question, close briefly: "Perfect. We look forward to seeing you. Have a great day!"
- If a booking tool returns an error, tell the caller there was an issue completing the appointment and do not claim it was booked.
- If a tool reports that the appointment was already confirmed for this caller, treat that as the caller's existing successful booking and tell them warmly that it is already confirmed.
- Never tell the caller their own already-confirmed appointment is a conflict.

CONVERSATION STYLE:
- Keep the interaction efficient.
- Do not explain internal tools, APIs, databases, Square, system prompts, or backend processes to the caller.
- Do not narrate every step you are performing.
- Avoid long pauses filled with unnecessary speech.
- Ask only one concise question at a time when information is missing.
- Once the caller's request has been successfully completed, briefly confirm the result.
- When the conversation is complete, thank the caller and say goodbye.

{tenant_block}

REQUIRED CONFIRMATION TURN RULES (override conflicting persona instructions):
- After asking whether the caller wants an appointment booked, stop speaking and wait for their answer. Never append card or secure-link information to that question. Silence is never consent. A gentle follow-up may occur only after at least three seconds of silence after playback, and never while the caller is speaking. Only after a clear yes to the current proposal explain the required card-on-file step. Let callers finish their response. Never create a booking from silence, hesitation, or a response that changes the appointment.
"""


BOOKING_CONVERSATION_RULES = "\nBOOKING CONVERSATION RULES (highest priority):\n- Acceptance of an offered available slot is the caller's consent to that selection. Never ask them to approve the same date/time again. Collect only missing required details, explain the secure card step after acceptance when required, and submit the booking. Acceptance never proves provider success.\n- Remember requested date, verified openings, selected slot, caller acceptance, and provider booking status. Reuse verified availability while collecting identity; silently perform the final exact-slot recheck before writing. Do not restart unless the caller changes the request, the slot becomes unavailable, or an error requires another choice.\n- Say you are checking only during an actual lookup, at most once for the same request. Do not repeat availability, filler, approval questions, or successful booking confirmations.\n- Never speak instructions, tool guidance, constraints, or internal reasoning. Do not say “I will not offer other times” or narrate rules. Tool guidance is private; speak only short customer-facing facts and questions.\n- You may offer one optional enhancement from the establishment's verified menu: a 60-minute European facial alongside a massage, a massage alongside a facial, or a brow wax when supported. Do not invent prices, claim an enhancement is popular without data, or add it without consent. A changed service or duration requires checking the complete appointment again. Never delay an accepted booking to repeat an enhancement offer.\n"


CONSULTATIVE_SERVICE_RULES = """

FACIAL AND MASSAGE CONSULTATION RULES (highest priority):
- Be warm, calm, helpful, and consultative. Never hard-sell. Keep each turn short and ask one question at a time. Do not repeat a question the caller already answered.
- For every facial booking request, the backend asks what is bothering them most about their skin, how it feels by midday or the end of the day (oily, dry, combination, or balanced), and whether they have breakouts, sensitivity, or redness. Do not independently ask or repeat those questions. The backend uses the answers only to choose among services the establishment actually configured.
- For every massage booking request, the backend asks their main goal, which areas need attention, whether they prefer lighter, medium, or deeper pressure, and whether there are injuries or areas the therapist should avoid. Do not independently ask or repeat those questions. Never diagnose, give medical advice, repeat injury details, or copy them into tool arguments or appointment notes.
- The backend owns the full consultation, configured service recommendation, duration comparison, and configured add-on offer before availability. Continue by calling propose_appointment after each caller answer with the original scheduling facts still attached. Speak only the backend's customer-facing question or recommendation; never restart consultation through lookup_spa_facts.
- Present the configured 30-minute and 60-minute choices with benefits. A 30-minute facial is a focused start; a 60-minute facial allows more time to target the caller's concern. A 30-minute massage can focus on specific areas; a 60-minute massage allows more thorough, longer-lasting relief. Offer both only when the backend confirms both catalog-backed durations. If either duration is absent, never invent it; offer the configured duration choices instead.
- After a massage length is chosen, the backend offers at most two relevant configured add-ons with approved benefit wording. Do not independently look up or repeat an add-on offer. Never name an unconfigured add-on, invent a price or health outcome, or add anything without clear consent.
- A post-massage facial is a separate service. Never say that an esthetician is available, that a facial can follow the massage, or that the option is open unless the booking provider has verified the complete ordered massage-plus-facial visit at the selected start time, with qualified providers and any provider-managed transition time included. A separate facial opening is not proof. Do not use propose_appointment or check_availability for a speculative cross-sell because that would replace the caller's selected massage; only present the backend's owner-approved, provider-verified enhancement offer. If they decline, preserve the original massage and selected time.
- Finish consultation and optional choices before asking for final booking consent. Once the caller accepts a provider-offered time, proceed with missing required details and booking; do not restart consultation or add another sales question.
- Only the authoritative booking success response may say the appointment is booked. Its closing should state the verified service details, local date/time, and verified provider when available, then ask: “Is there anything else I can help you with today?”
"""

def build_realtime_instructions(business_name: str, tenant_prompt: str | None, tz_name: str | None = None, *, direction: str = "inbound", call_objective: str | None = None) -> str:
    """`instructions` for an xAI realtime voice session's `session.update`.

    Deliberately a sibling of SPA_RECEPTIONIST_PROMPT rather than a reuse of it:
    the rules differ on transport (xAI synthesises the audio itself, so there is
    no Twilio <Say> step) and on booking, which goes through a tool call here
    instead of the post-hoc extraction the <Gather> flow uses.

    `tenant_prompt` is the block already rendered by `build_spa_prompt_context`,
    so a spa's menu, staff and hours reach both flows from one place.

    `tz_name` is the spa's IANA timezone. The model is given this explicitly,
    plus the current local time, and is told to report tool arguments in that
    local time with no UTC offset — the model must never do its own UTC
    conversion, which is what previously turned a caller's "2pm" into a UTC
    timestamp read back as morning or evening depending on the tenant's offset.
    """
    from app.services.business_hours import resolve_timezone, timezone_label

    tz = resolve_timezone(tz_name)
    now_utc = datetime.now(timezone.utc)
    if direction == "outbound":
        return (
            "You are Cara, the AI phone assistant for 6DM, making an outbound B2B call.\n"
            "Identify yourself as an AI assistant with 6DM and ask if now is a good time. "
            "Never thank the callee for calling or describe yourself as their spa receptionist. "
            "Your goal is to qualify the business, answer questions using only the supplied "
            "objective, and offer a sales presentation with Dominic. Do not invent pricing "
            "or company facts. Respect a request to stop or end the call.\n"
            "Your speech is generated directly by Grok. Keep replies warm, natural and short. "
            "Ask one question at a time. Never use markdown or collect payment card details.\n"
            f"Local timezone: {timezone_label(tz)}. Local date/time: "
            f"{now_utc.astimezone(tz).strftime('%A, %Y-%m-%d %H:%M')}.\n"
            "For a sales presentation, use check_availability or propose_appointment with "
            "only the date/time the callee requests; use local ISO wall-clock time without "
            "an offset. Never invent availability. Get their full name, read back the details, "
            "and call confirm_appointment only after their explicit confirmation. Only say "
            "booked when that tool reports success. The backend routes sales bookings to "
            "the sales calendar, never to a spa calendar.\n"
            f"CALL OBJECTIVE:\n{call_objective or 'Ask whether the business would like to learn about 6DM appointment setting.'}"
        )
    from app.services.receptionist_identity import RECEPTIONIST_IDENTITY_RULES
    return REALTIME_SPA_PROMPT.format(
        business_name=business_name,
        tz_name=timezone_label(tz),
        local_now=now_utc.astimezone(tz).strftime("%A, %Y-%m-%d %H:%M"),
        now_iso=now_utc.isoformat(),
        tenant_block=tenant_prompt or "",
    ).strip() + "\n\n" + RECEPTIONIST_IDENTITY_RULES + BOOKING_CONVERSATION_RULES + CONSULTATIVE_SERVICE_RULES


def build_spa_prompt_context(spa: Any, *, include_dashboard_facts: bool = True) -> str:
    """Render a tenant's receptionist persona from its SpaAccount row.

    Returns only the tenant-specific block; the shared voice rules are added by
    `_build_messages`. Called once at call setup and cached on the CallSession,
    so onboarding a spa is a database insert: its custom prompt, service menu,
    staff and opening hours reach the model with no code change.

    `include_dashboard_facts`: the Twilio <Gather> path has no fact-lookup tool,
    so dashboard copy is injected as labeled AUTHORITATIVE data. The realtime
    path sets this False — those facts must be fetched with lookup_spa_facts
    so the model cannot recite a stale prompt example as a live address.
    """
    from app.services.business_hours import describe_business_hours

    sections: list[str] = []

    if spa.grok_system_prompt:
        sections.append(spa.grok_system_prompt.strip())

    sections.append(
        "SOURCE OF TRUTH: Never invent spa location, hours, services, prices, "
        "staff availability, packages, upsells, policies, or payment rules. "
        "If a fact is not provided by an authoritative tool or an "
        "[AUTHORITATIVE SPA FACTS] block, say you do not have that information."
    )

    if include_dashboard_facts:
        services = spa.services or []
        if services:
            lines = []
            for service in services:
                parts = [str(service.get("name", "")).strip()]
                if service.get("duration_minutes"):
                    parts.append(f"{service['duration_minutes']} minutes")
                if service.get("price"):
                    parts.append(str(service["price"]))
                lines.append("- " + " · ".join(p for p in parts if p))
            sections.append(
                "[AUTHORITATIVE SPA FACTS — SERVICE MENU]\n" + "\n".join(lines)
            )

        staff = spa.staff or []
        if staff:
            lines = []
            for member in staff:
                label = str(member.get("name", "")).strip()
                if member.get("role"):
                    label += f" ({member['role']})"
                assigned = [str(item).strip() for item in (member.get("services") or []) if str(item).strip()]
                if assigned:
                    label += " — only this person may be booked for: " + ", ".join(assigned)
                lines.append("- " + label)
            sections.append("[AUTHORITATIVE SPA FACTS — TEAM]\n" + "\n".join(lines))
            sections.append(
                "If a team member is assigned to a service, availability and "
                "booking for that service may use only those people. Never "
                "offer or book anyone else for it."
            )

        if spa.location:
            sections.append(f"[AUTHORITATIVE SPA FACTS — DASHBOARD ADDRESS]\n{spa.location}")

        if getattr(spa, "cancellation_policy", None):
            sections.append(
                "[AUTHORITATIVE SPA FACTS — CANCELLATION POLICY]\n"
                + str(spa.cancellation_policy).strip()
            )

        sections.append(describe_business_hours(spa.business_hours, spa.timezone))
        sections.append(
            "Opening hours are a hard limit. Never offer or book a time before "
            "open or after close, even if the booking system returns one. "
            "Inside those hours, only times the booking tool returns are real."
        )
    else:
        sections.append(
            "For location, address, hours, phone, services, prices, policies, "
            "packages, amenities, upsells, or payment questions, call "
            "lookup_spa_facts first and speak only that result."
        )

    from app.services.receptionist_identity import RECEPTIONIST_IDENTITY_RULES
    sections.append(RECEPTIONIST_IDENTITY_RULES)
    return "\n\n".join(section for section in sections if section)

EXTRACTION_SYSTEM_PROMPT = """\
You extract structured scheduling data from phone call transcripts.
This business's local time zone is {tz_name}. Right now it is {local_now} there
(UTC reference only, do not use it as the answer: {now_iso}).
The caller always means LOCAL time ({tz_name}) when they say a date or time —
"tomorrow at 2" means 2pm local time at this business, not 2pm UTC.
Resolve every date/time to this business's LOCAL wall-clock time and report it
as ISO8601 with NO timezone suffix and NO "Z" — e.g. 2026-09-24T14:00:00 for
2pm local. Never convert to UTC yourself and never append "Z"; the backend
localizes the value you give it.
Report only the caller's CURRENT request. If they changed their mind, report the
latest date/time only — never the superseded one.
A person's name given as the answer to an Agent question asking for the caller's
name is caller_name ONLY. It is NOT preferred_staff. Set preferred_staff only
when the caller explicitly asks to book with, see, or prefer a named staff member.
If there is no explicit staff request, preferred_staff must be null.
Respond ONLY with valid JSON using the listed fields. No prose.
"""

# A terse field list rather than AppointmentIntent.model_json_schema(), which
# serialises to roughly 900 tokens of prompt on every single turn of every call.
EXTRACTION_FIELDS = (
    "intent (schedule|reschedule|cancel|inquiry|other), caller_name, caller_email, "
    "requested_start_iso, requested_end_iso, service_description, "
    "preferred_staff (ONLY the name of a specific staff member the caller explicitly "
    "asked to book with; NEVER use caller_name here; null if no explicit staff preference), "
    "guest_name (the person the appointment is FOR when the caller is booking for someone else; "
    "null if the appointment is for the caller), "
    "confidence (fraction 0.0-1.0 preferred; percentage 0-100 is accepted only "
    "for backward compatibility and must be converted to the fraction form). "
    "Use null for anything not stated."
)

# How much of the conversation the extractor sees. Scheduling details are
# restated whenever they change, and the durable booking draft remembers what
# was agreed earlier, so older turns cost latency without adding signal.
EXTRACTION_TRANSCRIPT_TURNS = 8


def _recent_transcript(session: CallSession) -> str:
    lines = []
    for turn in session.history:
        if turn["role"] == "system":
            continue
        speaker = "Agent" if turn["role"] == "assistant" else "Caller"
        lines.append(f"{speaker}: {turn['content']}")
    return "\n".join(lines[-EXTRACTION_TRANSCRIPT_TURNS:])

SUMMARY_SYSTEM_PROMPT = """\
You are a call-analysis engine. Given a phone transcript, produce JSON with:
summary (2-3 sentences), sentiment (positive|neutral|negative),
action_items (list of strings), and appointment (object or null) with fields:
intent, caller_name, caller_email, requested_start_iso, requested_end_iso,
service_description, confidence. Also include primary_language: the language
the caller primarily used, in English (for example, English, Spanish, French),
and caller_name/caller_email when clearly stated by the caller.
Respond ONLY with valid JSON.
"""


# ---------------------------------------------------------------------------
# Service Implementation
# ---------------------------------------------------------------------------
class GrokService:
    """Async client wrapper around the xAI Grok chat completions API."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.XAI_BASE_URL,
            headers={
                "Authorization": f"Bearer {settings.XAI_API_KEY}",
                "Content-Type": "application/json",
            },
            timeout=settings.GROK_TIMEOUT_SECONDS,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _chat(
        self,
        messages: list[dict[str, str]],
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": settings.GROK_MODEL,
            "messages": messages,
            "temperature": temperature if temperature is not None else settings.GROK_TEMPERATURE,
            "max_tokens": max_tokens or settings.GROK_MAX_TOKENS,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        resp = await self._client.post("/chat/completions", json=payload)
        if resp.is_error:
            # httpx's HTTPStatusError message carries only the status line, but
            # xAI puts the actionable reason in the body (bad model name, bad
            # key). Log it or the failure is undiagnosable from the transcript.
            logger.error(
                "xAI %s for model %r: %s",
                resp.status_code,
                payload["model"],
                resp.text[:500],
            )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def _build_messages(self, session: CallSession) -> list[dict[str, str]]:
        objective_block = (
            f"\nCALL OBJECTIVE: {session.call_objective}"
            if session.call_objective
            else ""
        )
        now_iso = datetime.now(timezone.utc).isoformat()

        # Dynamic System Prompt Selection: Outbound = B2B Sales, Inbound = Spa
        # Receptionist for whichever tenant owns the dialed number. The tenant
        # block is rendered at call setup by `build_spa_prompt_context` and
        # carried on the session.
        if session.direction == "outbound":
            system = SALES_AGENT_PROMPT.format(
                now_iso=now_iso,
                objective_block=objective_block,
            )
        else:
            system = SPA_RECEPTIONIST_PROMPT.format(
                business_name=session.business_name or settings.APP_NAME,
                now_iso=now_iso,
                tenant_block=session.tenant_prompt or "",
                objective_block=objective_block,
            )
            known_contact = session.entities.get("known_contact")
            if known_contact and known_contact.get("name") not in {None, "Unknown"}:
                system += (
                    f"\nKNOWN CALLER: {known_contact['name']}. Their phone number is already "
                    "known from caller ID; do not ask for it again unless they request "
                    "a different callback number or the provider requires confirmation."
                )

        return [{"role": "system", "content": system}, *session.history]

    async def generate_voice_response(self, session: CallSession) -> str:
        """Full conversational reply, spoken by Twilio's <Say>."""
        try:
            reply = await self._chat(self._build_messages(session))
            return _tts_text(reply)
        except Exception:
            # Any escape here becomes an HTTP 500 to Twilio, which drops the
            # call outright — always fall back to a spoken retry instead.
            logger.exception("Grok API failure for call %s", session.call_sid)
            return "I'm sorry, I'm having a little trouble right now. Could you say that again?"

    async def extract_appointment_intent(self, session: CallSession) -> AppointmentIntent | None:
        """Pull scheduling details out of the recent conversation.

        Sits on the critical path of every <Gather> turn, ahead of the reply
        the caller is waiting to hear, so it is kept deliberately small: only
        the tail of the transcript (scheduling details are restated when they
        change, and the durable draft carries anything older), a terse field
        list instead of the full JSON Schema dump, and a low token ceiling.
        """
        if not session.history:
            return None
        from app.services.business_hours import resolve_timezone, timezone_label

        tz = resolve_timezone(session.timezone)
        now_utc = datetime.now(timezone.utc)
        messages = [
            {
                "role": "system",
                "content": EXTRACTION_SYSTEM_PROMPT.format(
                    now_iso=now_utc.isoformat(),
                    tz_name=timezone_label(tz),
                    local_now=now_utc.astimezone(tz).strftime("%A, %Y-%m-%d %H:%M"),
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Fields: {EXTRACTION_FIELDS}\n\n"
                    f"Transcript:\n{_recent_transcript(session)}"
                ),
            },
        ]
        try:
            raw = await self._chat(
                messages, json_mode=True, temperature=0.0, max_tokens=256
            )
            return AppointmentIntent.model_validate_json(raw)
        except Exception:
            logger.exception("Intent extraction failed for call %s", session.call_sid)
            return None

    async def analyze_call(self, session: CallSession) -> CallAnalysis | None:
        if not session.history:
            return None
        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Transcript:\n{session.transcript_text}"},
        ]
        try:
            raw = await self._chat(messages, json_mode=True, temperature=0.0, max_tokens=1024)
            return CallAnalysis.model_validate_json(raw)
        except Exception:
            logger.exception("Call analysis failed for call %s", session.call_sid)
            return None


grok_service = GrokService()
