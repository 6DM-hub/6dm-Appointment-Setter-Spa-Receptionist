"""Webhook retries must preserve the audio provider selected for the call."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1.telephony import voice_inbound
from app.models import VoiceEngine, UserRole
from app.services.voice_config import resolve_xai_voice
from app.schemas.spa_account import SpaAccountUpdate
from pydantic import ValidationError
from fastapi import HTTPException


def test_voice_engine_patch_is_accepted_and_validated():
    update = SpaAccountUpdate(voice_engine="xai_realtime")
    assert update.model_dump(exclude_unset=True)["voice_engine"] is VoiceEngine.XAI_REALTIME
    assert "voice_engine" not in SpaAccountUpdate().model_dump(exclude_unset=True)
    for value in (None, "grok"):
        with pytest.raises(ValidationError):
            SpaAccountUpdate(voice_engine=value)


@pytest.mark.parametrize("role", [UserRole.SUPER_ADMIN, UserRole.SPA_ADMIN])
async def test_only_super_admin_can_change_voice_provider(role, monkeypatch):
    from app.api.v1 import spa_accounts

    spa = SimpleNamespace(voice_engine=VoiceEngine.TWILIO_TTS)
    monkeypatch.setattr(spa_accounts, "_load_visible_spa", AsyncMock(return_value=spa))
    monkeypatch.setattr(spa_accounts, "_to_read", lambda value: value)
    db = SimpleNamespace(commit=AsyncMock(), refresh=AsyncMock())
    kwargs = dict(
        spa_id="tenant", payload=SpaAccountUpdate(voice_engine="xai_realtime"),
        current_user=SimpleNamespace(role=role), db=db,
    )
    if role is UserRole.SUPER_ADMIN:
        result = await spa_accounts.update_spa_account(**kwargs)
        assert result.voice_engine is VoiceEngine.XAI_REALTIME
        db.commit.assert_awaited_once()
    else:
        with pytest.raises(HTTPException) as error:
            await spa_accounts.update_spa_account(**kwargs)
        assert error.value.status_code == 403
        assert spa.voice_engine is VoiceEngine.TWILIO_TTS
        db.commit.assert_not_awaited()


@pytest.mark.parametrize("value,expected", [
    ("xai_ara", "ara"), ("Ara", "ara"), (" EVE ", "eve"),
    ("xai_rex", "rex"), ("custom-Voice-ID", "custom-Voice-ID"),
    ("Carina", "Carina"),
])
def test_builtin_aliases_preserve_custom_voice_ids(value, expected):
    assert resolve_xai_voice(value) == expected


@pytest.mark.parametrize("engine", [VoiceEngine.XAI_REALTIME, VoiceEngine.TWILIO_TTS])
async def test_duplicate_inbound_preserves_provider_and_voice(engine):
    row = SimpleNamespace(tenant_id="tenant")
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=SimpleNamespace(voice_engine=engine))
    state = MagicMock()
    state.get = AsyncMock(return_value=SimpleNamespace(voice="Polly.Joanna"))

    response = await voice_inbound(
        CallSid="CAretry", From="+15550000001", To="+15550000002", db=db, state=state,
    )
    xml = response.body.decode()
    if engine is VoiceEngine.XAI_REALTIME:
        assert "<Connect>" in xml
        assert "media-stream/CAretry" in xml
        assert "<Say" not in xml
    else:
        assert '<Say voice="Polly.Joanna">' in xml
        assert "<Gather" in xml
