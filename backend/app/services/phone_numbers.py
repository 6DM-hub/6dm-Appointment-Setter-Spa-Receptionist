"""Phone-number normalisation shared by every inbound call path.

Twilio reports the dialed party differently depending on how the call arrived:
a plain PSTN call gives `+18058878345`, while a call arriving over an Elastic
SIP trunk gives `sip:+18058878345@sip.voice.x.ai;transport=tls`. Tenant routing
keys on `SpaAccount.twilio_phone_number`, so both have to reduce to the same
E.164 string or a SIP-trunked call resolves to no tenant and its CallLog row
ends up owned by nobody.
"""

__all__ = [
    "canonical_customer_phone",
    "is_non_phone_caller_id",
    "normalize_phone_target",
]


def normalize_phone_target(value: str | None) -> str | None:
    """Reduce a SIP URI or bracketed address to the bare E.164 number.

    `sip:+18058878345@sip.voice.x.ai;transport=tls`, `<+18058878345>` and
    `+18058878345` all return `+18058878345`. Returns None for empty input so
    callers can fall back rather than query on a meaningless value.
    """
    if not value:
        return None

    text = value.strip().strip("<>")
    if ";" in text:
        text = text.split(";", 1)[0]

    lowered = text.lower()
    if lowered.startswith("sip:"):
        text = text[4:]
    elif lowered.startswith("sips:"):
        text = text[5:]

    if "@" in text:
        text = text.split("@", 1)[0]

    text = text.strip()
    if text.startswith("+"):
        text = "+" + "".join(character for character in text[1:] if character.isdigit())
    return text or None


_NON_PHONE_CALLER_IDS = frozenset({
    "anonymous",
    "blocked",
    "empty",
    "no caller id",
    "nocallerid",
    "none",
    "null",
    "private",
    "restricted",
    "unavailable",
    "unknown",
    "withheld",
})

# Timezones whose local numbers use the North American Numbering Plan (+1).
# A spa in another country must not have a 10-digit number rewritten as +1.
_NANP_PREFIXES = (
    "America/Indiana/",
    "America/Kentucky/",
    "America/North_Dakota/",
    "Canada/",
    "US/",
)
_NANP_TIMEZONES = frozenset({
    "America/Adak",
    "America/Anchorage",
    "America/Atikokan",
    "America/Blanc-Sablon",
    "America/Boise",
    "America/Cambridge_Bay",
    "America/Chicago",
    "America/Coral_Harbour",
    "America/Creston",
    "America/Dawson",
    "America/Dawson_Creek",
    "America/Denver",
    "America/Detroit",
    "America/Edmonton",
    "America/Fort_Nelson",
    "America/Glace_Bay",
    "America/Goose_Bay",
    "America/Halifax",
    "America/Inuvik",
    "America/Iqaluit",
    "America/Juneau",
    "America/Los_Angeles",
    "America/Menominee",
    "America/Metlakatla",
    "America/Moncton",
    "America/New_York",
    "America/Nipigon",
    "America/Nome",
    "America/Phoenix",
    "America/Puerto_Rico",
    "America/Rainy_River",
    "America/Rankin_Inlet",
    "America/Regina",
    "America/Resolute",
    "America/Sitka",
    "America/St_Johns",
    "America/Swift_Current",
    "America/Thunder_Bay",
    "America/Toronto",
    "America/Vancouver",
    "America/Whitehorse",
    "America/Winnipeg",
    "America/Yakutat",
    "America/Yellowknife",
    "Pacific/Honolulu",
})


def is_non_phone_caller_id(value: str | None) -> bool:
    """True for withheld, private, or otherwise non-dialable caller IDs."""
    if value is None:
        return True
    text = " ".join(value.strip().casefold().split())
    if not text:
        return True
    return text in _NON_PHONE_CALLER_IDS


def _uses_nanp(timezone_name: str | None) -> bool:
    if not timezone_name:
        return False
    tz = timezone_name.strip()
    if tz in _NANP_TIMEZONES or tz.startswith(_NANP_PREFIXES):
        return True
    return False


def canonical_customer_phone(
    value: str | None,
    *,
    timezone_name: str | None = None,
) -> str | None:
    """One E.164 value for customer lookup, or None when it is not a phone.

    Numbers that already include a country code are kept. A 10-digit national
    number is prefixed with +1 only when this spa's timezone is in the NANP.
    Withheld caller IDs are not returned as phone numbers.
    """
    if is_non_phone_caller_id(value):
        return None

    raw = normalize_phone_target(value)
    if is_non_phone_caller_id(raw):
        return None
    if not raw:
        return None

    if raw.startswith("+"):
        digits = raw[1:]
        if digits.isdigit() and 8 <= len(digits) <= 15:
            return "+" + digits
        return None

    digits = "".join(character for character in raw if character.isdigit())
    if not digits:
        return None
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if len(digits) == 10 and _uses_nanp(timezone_name):
        return "+1" + digits
    return None
