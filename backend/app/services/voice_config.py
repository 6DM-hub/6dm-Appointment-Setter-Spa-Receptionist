"""Normalize legacy aliases without changing custom xAI voice IDs."""

BUILTIN_XAI_VOICES = frozenset({"ara", "eve", "leo", "rex", "sal"})


def resolve_xai_voice(value: str) -> str:
    voice = value.strip()
    candidate = voice.lower().removeprefix("xai_")
    if candidate in BUILTIN_XAI_VOICES:
        return candidate
    return voice
