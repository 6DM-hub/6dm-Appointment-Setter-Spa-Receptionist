"""per-spa voice engine selection

Adds `spa_accounts.voice_engine`, choosing how a tenant's inbound calls are
answered:

  * `twilio_tts`   — Twilio <Gather> speech recognition plus <Say> playback,
                     with Grok generating each reply as text.
  * `xai_realtime` — Twilio <Connect><Stream> bridged into xAI's realtime
                     speech-to-speech socket, which hears and speaks directly.

New tenants default to `xai_realtime`, while existing rows retain their stored
engine value; set `XAI_REALTIME_ENABLED=false` to honor the legacy engine for
tenants that have not opted into realtime.

Revision ID: c3d4e5f60a11
Revises: b7f2a91c4d30
Create Date: 2026-09-02 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f60a11"
down_revision: Union[str, Sequence[str], None] = "b7f2a91c4d30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

VOICE_ENGINES = ("twilio_tts", "xai_realtime")


def upgrade() -> None:
    voice_engine = sa.Enum(*VOICE_ENGINES, name="voice_engine")
    voice_engine.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "spa_accounts",
        sa.Column(
            "voice_engine",
            voice_engine,
            nullable=False,
            server_default="xai_realtime",
        ),
    )


def downgrade() -> None:
    op.drop_column("spa_accounts", "voice_engine")
    sa.Enum(name="voice_engine").drop(op.get_bind(), checkfirst=True)
