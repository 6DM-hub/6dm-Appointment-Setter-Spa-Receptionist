"""Public card-entry routes. The link token is not a dashboard login."""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.services.booking_adapters.spa_router import SpaBookingAdapter
from app.services.card_entry import (
    PUBLIC_REJECTION,
    SqlCardEntryStore,
    hash_card_token,
    session_payload,
    session_state,
    submit_saved_card,
)
from app.services.card_entry_page import CARD_PAGE_HTML
from datetime import datetime, timezone

page_router = APIRouter()
router = APIRouter(prefix="/card-entry", tags=["card-entry"])

_HITS: dict[str, deque[float]] = defaultdict(deque)
_LIMIT = 30
_WINDOW_SECONDS = 60

_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://web.squarecdn.com https://sandbox.web.squarecdn.com; "
        "frame-src https://web.squarecdn.com https://sandbox.web.squarecdn.com; "
        "connect-src 'self' https://pci-connect.squareup.com https://pci-connect.squareupsandbox.com "
        "https://o160250.ingest.sentry.io; "
        "style-src 'self' 'unsafe-inline' https://web.squarecdn.com https://sandbox.web.squarecdn.com; "
        "img-src 'self' data: https://web.squarecdn.com https://sandbox.web.squarecdn.com; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
}


class _TokenBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=20, max_length=200)


class _SaveBody(_TokenBody):
    source_id: str = Field(min_length=4, max_length=256)
    verification_token: str | None = Field(default=None, max_length=4096)

    @field_validator("source_id", "verification_token")
    @classmethod
    def reject_raw_card(cls, value: str | None) -> str | None:
        if value and _digits_only_card(value):
            raise ValueError("invalid")
        return value


def _digits_only_card(value: str) -> bool:
    compact = "".join(ch for ch in value if ch.isdigit())
    return len(compact) >= 13 and compact == "".join(value.split())


def _json(status_code: int, body: dict) -> JSONResponse:
    response = JSONResponse(status_code=status_code, content=body)
    for key, value in _SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


def _reject(status_code: int = 404) -> JSONResponse:
    return _json(status_code, {"detail": PUBLIC_REJECTION})


def _limit(request: Request) -> JSONResponse | None:
    host = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _HITS[host]
    while bucket and now - bucket[0] > _WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= _LIMIT:
        return _reject(429)
    bucket.append(now)
    return None


def _headers(response):
    for key, value in _SECURITY_HEADERS.items():
        response.headers[key] = value
    return response


@page_router.get("/card")
async def card_page():
    response = HTMLResponse(CARD_PAGE_HTML)
    return _headers(response)


@router.post("/session")
async def card_session(
    body: _TokenBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    limited = _limit(request)
    if limited is not None:
        return limited
    store = SqlCardEntryStore(db)
    loaded = await store.find_by_hash(hash_card_token(body.token))
    if loaded is None:
        return _reject()
    state = session_state(loaded, datetime.now(timezone.utc))
    if state == "already_on_file":
        if loaded.token.revoked_at is None:
            loaded.token.revoked_at = datetime.now(timezone.utc)
            await store.persist()
        response_body = {"spa_name": loaded.spa.name, "state": "already_on_file"}
    elif state != "ready":
        return _reject()
    else:
        response_body = session_payload(loaded)
    if "access_token" in response_body or "external_customer_id" in response_body:
        return _reject()
    return _json(200, response_body)


@router.post("/save")
async def card_save(
    body: _SaveBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    limited = _limit(request)
    if limited is not None:
        return limited
    store = SqlCardEntryStore(db)
    loaded = await store.find_by_hash(hash_card_token(body.token))
    if loaded is None:
        return _reject()
    adapter = SpaBookingAdapter(loaded.spa)
    outcome = await submit_saved_card(
        store=store,
        loaded=loaded,
        source_id=body.source_id,
        verification_token=body.verification_token,
        adapter=adapter,
    )
    if outcome == "saved":
        return _json(200, {"state": "saved", "detail": "Your card was saved securely."})
    if outcome == "already_on_file":
        return _json(200, {"state": "already_on_file", "detail": "A card is already on file for this appointment."})
    if outcome == "busy":
        return _json(200, {"state": "busy", "detail": "This card is already being saved. Please wait a moment."})
    if outcome == "ambiguous":
        return _json(200, {
            "state": "ambiguous",
            "detail": "We could not confirm the card was saved. Please try this link again.",
        })
    if outcome == "rejected":
        return _json(200, {
            "state": "rejected",
            "detail": "The card could not be saved. You can try again with this link.",
        })
    return _reject()
