"""Per-call spoken-language continuity for Cara's realtime voice sessions.

The realtime model can understand several languages automatically, but backend
owned booking/card/recovery lines are sent with ``force_message`` and therefore
must be translated explicitly.  This module keeps language recognition small,
deterministic, and independent from booking data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CallLanguage:
    name: str
    code: str


SUPPORTED_CALL_LANGUAGES: dict[str, CallLanguage] = {
    "en": CallLanguage("English", "en"),
    "ar-EG": CallLanguage("Arabic", "ar-EG"),
    "bn": CallLanguage("Bengali", "bn"),
    "zh": CallLanguage("Chinese", "zh"),
    "fr": CallLanguage("French", "fr"),
    "de": CallLanguage("German", "de"),
    "hi": CallLanguage("Hindi", "hi"),
    "id": CallLanguage("Indonesian", "id"),
    "it": CallLanguage("Italian", "it"),
    "ja": CallLanguage("Japanese", "ja"),
    "ko": CallLanguage("Korean", "ko"),
    "pt-BR": CallLanguage("Portuguese", "pt-BR"),
    "ru": CallLanguage("Russian", "ru"),
    "es-MX": CallLanguage("Spanish", "es-MX"),
    "tr": CallLanguage("Turkish", "tr"),
    "vi": CallLanguage("Vietnamese", "vi"),
}

_ALIASES = {
    "english": "en", "ingles": "en", "inglés": "en",
    "arabic": "ar-EG", "árabe": "ar-EG", "arabic egypt": "ar-EG",
    "bengali": "bn", "bangla": "bn",
    "chinese": "zh", "mandarin": "zh", "中文": "zh", "普通话": "zh",
    "french": "fr", "français": "fr", "francais": "fr",
    "german": "de", "deutsch": "de",
    "hindi": "hi", "हिन्दी": "hi", "हिंदी": "hi",
    "indonesian": "id", "bahasa indonesia": "id",
    "italian": "it", "italiano": "it",
    "japanese": "ja", "日本語": "ja",
    "korean": "ko", "한국어": "ko",
    "portuguese": "pt-BR", "português": "pt-BR", "portugues": "pt-BR",
    "russian": "ru", "русский": "ru",
    "spanish": "es-MX", "español": "es-MX", "espanol": "es-MX",
    "turkish": "tr", "türkçe": "tr", "turkce": "tr",
    "vietnamese": "vi", "tiếng việt": "vi", "tieng viet": "vi",
}

_EXPLICIT_REQUEST = re.compile(
    r"(?:speak|talk|continue|switch|change|answer|respond|habla(?:r)?|hable|"
    r"podemos hablar|en|parler|répondre|falar|responder|sprechen|antworten)"
    r"[^.!?]{0,36}\b(" + "|".join(
        re.escape(alias) for alias in sorted(_ALIASES, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)

# Latin-script auto-detection deliberately requires several markers.  A single
# borrowed word, treatment name, or customer/staff name must never flip a call.
_WORD_MARKERS: dict[str, set[str]] = {
    "es-MX": {
        "que", "quiero", "quisiera", "necesito", "cita", "para", "por",
        "favor", "gracias", "mañana", "tarde", "masaje", "facial", "hora",
        "disponible", "disponibilidad", "reservar", "cancelar", "cambiar",
        "puede", "puedo", "tienen", "tengo", "con", "una", "un", "hoy",
    },
    "fr": {
        "bonjour", "merci", "voudrais", "rendez-vous", "demain", "massage",
        "disponible", "disponibilité", "annuler", "changer", "avec", "pour",
        "une", "heure", "soir", "matin", "pouvez", "je", "vous",
    },
    "pt-BR": {
        "olá", "obrigado", "obrigada", "gostaria", "marcar", "horário",
        "amanhã", "massagem", "disponível", "cancelar", "mudar", "com",
        "para", "uma", "hoje", "tarde", "manhã", "você", "tem",
    },
    "de": {
        "hallo", "danke", "möchte", "termin", "morgen", "massage", "frei",
        "verfügbar", "absagen", "ändern", "mit", "für", "heute", "uhr",
        "bitte", "können", "ich", "sie",
    },
    "it": {
        "buongiorno", "grazie", "vorrei", "appuntamento", "domani",
        "massaggio", "disponibile", "annullare", "cambiare", "con", "per",
        "una", "oggi", "pomeriggio", "mattina", "potete", "sono",
    },
    "id": {
        "halo", "terima", "kasih", "ingin", "janji", "besok", "pijat",
        "tersedia", "batalkan", "ubah", "dengan", "untuk", "hari", "jam",
        "bisa", "saya", "ada",
    },
    "tr": {
        "merhaba", "teşekkür", "istiyorum", "randevu", "yarın", "masaj",
        "müsait", "iptal", "değiştir", "ile", "için", "bugün", "saat",
        "lütfen", "ben", "bir",
    },
    "vi": {
        "xin", "chào", "cảm", "ơn", "muốn", "hẹn", "ngày", "mai", "mát",
        "xa", "trống", "hủy", "đổi", "với", "cho", "hôm", "nay", "giờ",
        "tôi", "có",
    },
}


def normalize_call_language(value: str | None) -> CallLanguage | None:
    """Resolve a supported BCP-47 code or human language name."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw in SUPPORTED_CALL_LANGUAGES:
        return SUPPORTED_CALL_LANGUAGES[raw]
    lowered = raw.casefold()
    for code, language in SUPPORTED_CALL_LANGUAGES.items():
        if lowered in {code.casefold(), language.name.casefold()}:
            return language
    code = _ALIASES.get(lowered)
    return SUPPORTED_CALL_LANGUAGES.get(code) if code else None


def detect_requested_call_language(text: str | None) -> CallLanguage | None:
    """Detect an explicit language request or substantial non-English speech.

    English remains the default and is intentionally not auto-locked.  This
    lets a caller greet Cara in English and then ask to continue in Spanish.
    Once a non-English language is locked, the live session owns that decision.
    """
    value = str(text or "").strip()
    if not value:
        return None

    explicit = _EXPLICIT_REQUEST.search(value)
    if explicit:
        return normalize_call_language(explicit.group(1))

    # Strong Unicode-script evidence. These scripts do not occur in ordinary
    # English service/provider names, so one substantive phrase is sufficient.
    compact = re.sub(r"[\W\d_]", "", value, flags=re.UNICODE)
    if len(compact) >= 3:
        if re.search(r"[\u0600-\u06ff]", value):
            return SUPPORTED_CALL_LANGUAGES["ar-EG"]
        if re.search(r"[\u0980-\u09ff]", value):
            return SUPPORTED_CALL_LANGUAGES["bn"]
        if re.search(r"[\u0900-\u097f]", value):
            return SUPPORTED_CALL_LANGUAGES["hi"]
        if re.search(r"[\u3040-\u30ff]", value):
            return SUPPORTED_CALL_LANGUAGES["ja"]
        if re.search(r"[\uac00-\ud7af]", value):
            return SUPPORTED_CALL_LANGUAGES["ko"]
        if re.search(r"[\u4e00-\u9fff]", value):
            return SUPPORTED_CALL_LANGUAGES["zh"]
        if re.search(r"[\u0400-\u04ff]", value):
            return SUPPORTED_CALL_LANGUAGES["ru"]

    words = set(re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)?", value.casefold(), re.UNICODE))
    best_code = None
    best_score = 0
    for code, markers in _WORD_MARKERS.items():
        score = len(words & markers)
        if score > best_score:
            best_code, best_score = code, score
    return SUPPORTED_CALL_LANGUAGES[best_code] if best_code and best_score >= 3 else None


def locked_language_instruction(language: CallLanguage) -> str:
    return (
        "\n\nCALL LANGUAGE — LOCKED FOR THE REST OF THIS CALL:\n"
        f"- Speak only {language.name} ({language.code}) in every caller-facing response.\n"
        "- Do not switch back to English because the caller says a name, treatment, "
        "date, time, yes/no, or another short English phrase.\n"
        "- Keep business, service, staff, and product names exactly as configured.\n"
        "- Tool names, tool arguments, IDs, and provider data remain in their canonical "
        "form; translate only what the caller hears.\n"
        "- This language lock may not be changed again during this call."
    )
