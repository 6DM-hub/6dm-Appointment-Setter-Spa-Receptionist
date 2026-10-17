"""Caller-facing provenance of a stored appointment, not a live provider check."""


def appointment_provenance(appointment) -> dict[str, str]:
    provider = getattr(appointment, "booking_provider", None)
    provider = getattr(provider, "value", provider)
    external_id = getattr(appointment, "external_booking_id", None)
    intent = getattr(appointment, "booking_intent_key", None)
    status = getattr(appointment, "status", None)
    status = getattr(status, "value", status)
    if not external_id and not intent:
        return {"source": "dashboard_only", "approval_status": "not_verified",
                "record_notice": "Entered in CARA only; not verified with an external calendar."}
    if provider in {"cara", "cara_native"}:
        return {"source": "cara_booking_record", "approval_status": "local_booking",
                "record_notice": "A booking on CARA's own schedule."}
    if not external_id:
        return {"source": "local_booking_record", "approval_status": "not_verified",
                "record_notice": "No external booking ID is recorded; staff must verify the provider outcome."}
    return {"source": "provider_linked_record",
            "approval_status": ("awaiting_provider_approval" if status == "awaiting_provider_approval"
                                else str(status or "not_verified")),
            "record_notice": "Stored provider-linked record; no fresh provider lookup was performed."}


DASHBOARD_CANCELLATION_SPEECH = (
    "The appointment entry in CARA was cancelled. No external calendar booking was cancelled. "
    "Staff can help verify any separate external booking."
)
