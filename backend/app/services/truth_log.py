"""Structured source-of-truth logs for live receptionist calls.

Event names are stable so Railway grep can prove whether a spoken fact came
from the dashboard, Square, or was refused as unknown. Never include access
tokens, card numbers, CVV, or other credentials.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("receptionist.truth")


def truth(event: str, **fields: Any) -> None:
    """Emit one `EVENT key=value ...` line with non-secret fields only."""
    parts = [event]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        text = str(value).replace("\n", " ").strip()
        if len(text) > 400:
            text = text[:397] + "..."
        parts.append(f"{key}={text}")
    logger.info(" ".join(parts))
