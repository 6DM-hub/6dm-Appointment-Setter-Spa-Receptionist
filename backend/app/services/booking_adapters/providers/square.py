import asyncio
import hashlib
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any

import httpx

from app.core.config import settings
from app.services.booking_adapters.base import (
    AvailabilityVerdict,
    BookingContext,
    BookingProviderError,
    ExternalBooking,
)
from app.services.booking_adapters.customers import (
    CustomerLookupOutcome,
    CustomerLookupResult,
    ProviderCustomer,
    ambiguous_customer_message,
    caller_profile_name,
    unique_customer_by_name,
)
from app.services.booking_adapters.saved_payments import (
    SaveCardOutcome,
    SaveCardResult,
    SavedPaymentLookup,
    SavedPaymentLookupOutcome,
)
from app.services.booking_adapters.providers.base import VerticalProviderAdapter
from app.services.phone_numbers import canonical_customer_phone
from app.services.spa_facts import format_square_address
from app.services.truth_log import truth

logger = logging.getLogger(__name__)


def _raw_card_material(value: str) -> bool:
    """True when a value is a run of card digits rather than a provider token."""
    compact = "".join(ch for ch in value if ch.isdigit())
    return len(compact) >= 13 and compact == "".join(value.split())

_NUMBER_WORD_PATTERN = r"(?:(?:(?:one|two|three|four|five|six|seven|eight|nine))[\s-]+hundred(?:[\s-]+(?:and[\s-]+)?(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?|(?:ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)|(?:one|two|three|four|five|six|seven|eight|nine)))?|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?|(?:ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)|(?:one|two|three|four|five|six|seven|eight|nine)|hundred)"
_DURATION_PHRASE_RE = re.compile(
    rf"\b(?:\d+(?:\.\d+)?|{_NUMBER_WORD_PATTERN})"
    r"\s*[-–—]?\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)


class ServiceResolutionError(ValueError):
    """The caller's wording does not clearly map to an approved spa service."""

    def __init__(self, reason: str, *, service_name: str | None = None) -> None:
        self.reason = reason
        self.service_name = service_name
        super().__init__(reason)


class TeamMemberResolutionError(ValueError):
    """The caller asked for a specific staff member who can't be matched."""

    def __init__(self, reason: str, *, staff_name: str | None = None) -> None:
        self.reason = reason
        self.staff_name = staff_name
        super().__init__(reason)


def _guest_booking_note(ctx: BookingContext) -> str | None:
    """A booking note for the guest. Never a reason to edit the caller profile."""
    guest = " ".join((ctx.guest_name or "").split())
    if not guest:
        return None
    caller = caller_profile_name(
        caller_name=ctx.caller_name,
        guest_name=ctx.guest_name,
        customer_name=ctx.customer_name,
    )
    if caller and guest.casefold() == caller.casefold():
        return None
    return f"Guest: {guest}"


_NAME_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def _normalize_person_name(value: str | None) -> str:
    """Compare names without case, extra spaces, or harmless punctuation."""
    text = _NAME_PUNCTUATION.sub(" ", value or "")
    return " ".join(text.split()).casefold()


def _spoken_caller_name(ctx: BookingContext) -> str | None:
    spoken = caller_profile_name(
        caller_name=ctx.caller_name,
        guest_name=ctx.guest_name,
        customer_name=ctx.customer_name,
    )
    return " ".join((spoken or "").split()) or None


def _caller_name_mismatch_line(
    ctx: BookingContext,
    *,
    on_file: str | None,
    resolution: str | None,
) -> str | None:
    """Staff note when a reused Square customer is not the spoken caller.

    A new customer is created from the spoken name, so that case has nothing
    to annotate. Matching after normalization is the same person.
    """
    if resolution != "reused":
        return None
    spoken = _spoken_caller_name(ctx)
    if not spoken or not _normalize_person_name(spoken):
        return None
    if _normalize_person_name(spoken) == _normalize_person_name(on_file):
        return None
    return f"Caller name provided during call: {spoken}"


def _square_customer_note(
    ctx: BookingContext,
    *,
    on_file: str | None,
    resolution: str | None,
) -> tuple[str | None, bool]:
    """Square `customer_note`, plus whether a caller-name line was added.

    Guest notes already use this field. A system call stamp is not a staff
    note and is not copied over. Any other existing note is kept, and the
    mismatch line is appended on its own line.
    """
    mismatch = _caller_name_mismatch_line(ctx, on_file=on_file, resolution=resolution)
    guest = _guest_booking_note(ctx)
    raw = (ctx.notes or "").strip()
    if raw.startswith("Booked by the AI agent during call "):
        raw = ""
    if mismatch is None:
        return guest, False
    pieces: list[str] = []
    if raw:
        pieces.append(raw)
    if guest and guest not in raw:
        pieces.append(guest)
    line = mismatch
    if line not in pieces:
        pieces.append(line)
    return "\n".join(pieces), True


class SquareAdapter(VerticalProviderAdapter):
    """
    Square Bookings API adapter.

    Responsibilities:
    - Resolve Square appointment service variation
    - Search REAL Square availability
    - Verify requested slot actually exists
    - Find/create Square customer
    - Create booking with Square appointment segments
    - Use stable idempotency keys
    - Re-check availability before booking
    - Retrieve booking version before update/cancel
    - Reschedule using Square availability
    - Cancel using optimistic concurrency

    Secrets stay backend-only.
    """

    provider = "square"
    required_config_keys = ("access_token", "location_id")
    api_docs = "https://developer.squareup.com/reference/square/bookings-api"
    implemented = True
    supports_customer_lookup = True
    supports_customer_creation = True
    supports_saved_payment_method_lookup = True
    supports_save_card_on_file = True

    def __init__(
        self,
        spa_name: str,
        config: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(spa_name, config)

        self.square_config = dict(config or {})

        self.access_token = str(
            self.square_config.get("access_token") or ""
        ).strip()

        self.location_id = str(
            self.square_config.get("location_id") or ""
        ).strip()

        self.environment = str(
            self.square_config.get("environment")
            or getattr(settings, "SQUARE_ENVIRONMENT", "production")
        ).lower()

        self.api_version = str(
            self.square_config.get("api_version")
            or getattr(settings, "SQUARE_API_VERSION", "2026-09-16")
        )

        self.base_url = (
            "https://connect.squareupsandbox.com"
            if self.environment == "sandbox"
            else "https://connect.squareup.com"
        )

        # Provider facts are authoritative for an active Square-backed call.
        # Cache the configured location for the lifetime of this adapter so
        # availability/create can reuse the same verified location + timezone
        # without repeatedly trusting stale tenant metadata.
        self.timezone_name: str | None = None
        self._resolved_customer: ProviderCustomer | None = None
        self._customer_resolution: str | None = None
        self._recovered_existing = False
        self._location_cache: dict[str, Any] | None = None
        # Catalog item resolution is stable for a booking draft; do not search
        # Square Catalog again for the same caller phrase on this adapter.
        self._service_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._last_range_slots: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # COMMON HELPERS
    # ------------------------------------------------------------------

    def _validate_config(self) -> None:
        if not self.access_token:
            raise BookingProviderError(
                "Square access token is not configured for this tenant"
            )

        if not self.location_id:
            raise BookingProviderError(
                "Square location is not configured for this tenant"
            )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Square-Version": self.api_version,
        }

    async def _location(self) -> dict[str, Any]:
        """Return the configured Square location after validating it live.

        A non-empty ``location_id`` in our tenant config is not enough: the
        location may have been deactivated, deleted, or belong to a different
        environment.  All operational booking decisions therefore validate the
        configured location against Square before using it.
        """
        if self._location_cache is not None:
            return self._location_cache

        data = await self._request("GET", f"/v2/locations/{self.location_id}")
        location = data.get("location") or {}
        if not location.get("id"):
            raise BookingProviderError("Square did not return the configured location")
        if str(location.get("id")) != self.location_id:
            raise BookingProviderError("Square returned a different location than configured")
        if str(location.get("status") or "").upper() != "ACTIVE":
            raise BookingProviderError("The configured Square location is not active")

        self._location_cache = location
        truth(
            "LOCATION_VERIFIED",
            provider="square",
            location_id=self.location_id,
            location_name=location.get("name"),
            timezone=location.get("timezone"),
            status=location.get("status"),
            address=format_square_address(location),
        )
        logger.info(
            "Square location verified: spa=%s location_id=%s timezone=%s name=%r",
            self.spa_name,
            self.location_id,
            location.get("timezone"),
            location.get("name"),
        )
        return location

    async def describe_location(self) -> dict[str, Any]:
        """Live Square Location used for address/timezone answers and bookings."""
        return await self._location()

    async def booking_timezone_name(self) -> str | None:
        """Square Location timezone once it has been live-verified.

        Do not GET /v2/locations here: parsing a caller time must not block
        (or fail) service clarification. Live verification still happens on
        SearchAvailability / create / move / lookup_spa_facts.
        """
        if self._location_cache is None:
            return None
        value = str(self._location_cache.get("timezone") or "").strip()
        return value or None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_config()

        url = f"{self.base_url}{path}"
        safe_to_retry = method.upper() == "GET" or path in {
            "/v2/bookings/availability/search",
            "/v2/team-members/search",
            "/v2/customers/search",
        }
        attempts = 2 if safe_to_retry else 1

        # Below `CALENDAR_TIMEOUT_SECONDS`. Booking and customer creates are
        # not retried: a timeout can mean Square already accepted the write.
        # Reads and availability search may be tried once more.
        response = None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    response = await client.request(
                        method,
                        url,
                        headers=self._headers(),
                        json=json,
                        params=params,
                    )
            except httpx.RequestError as exc:
                if attempt + 1 >= attempts:
                    logger.exception(
                        "Square network error spa=%s path=%s",
                        self.spa_name,
                        path,
                    )
                    raise BookingProviderError(
                        "Unable to communicate with Square",
                        retryable=True,
                    ) from exc
                await asyncio.sleep(0.25)
                continue
            retry_status = response.status_code == 429 or response.status_code >= 500
            if response.is_error and safe_to_retry and retry_status and attempt + 1 < attempts:
                await asyncio.sleep(0.25)
                continue
            break

        if response is None:
            raise BookingProviderError(
                "Unable to communicate with Square",
                retryable=True,
            )

        if response.is_error:
            detail = self._safe_square_error(response)
            error_code = self._first_square_error_code(response)

            logger.warning(
                "Square API error spa=%s path=%s status=%s error=%s code=%s",
                self.spa_name,
                path,
                response.status_code,
                detail,
                error_code,
            )

            raise BookingProviderError(
                f"Square API request failed: {detail}",
                code=error_code,
                retryable=response.status_code == 429 or response.status_code >= 500,
            )

        # Explicit success-status logging for the two calls that decide
        # whether a caller hears a real booking outcome. No token/credential
        # values are ever included here.
        if path == "/v2/bookings/availability/search":
            logger.info("Square availability HTTP status=%s", response.status_code)
        elif method.upper() == "POST" and path == "/v2/bookings":
            logger.info("Square create booking HTTP status=%s", response.status_code)

        if not response.content:
            return {}

        try:
            return response.json()
        except ValueError as exc:
            raise BookingProviderError(
                "Square returned an invalid response"
            ) from exc

    @staticmethod
    def _safe_square_error(response: httpx.Response) -> str:
        """
        Extract useful Square error information without logging credentials.
        """
        try:
            data = response.json()
        except ValueError:
            return f"HTTP {response.status_code}"

        errors = data.get("errors") or []

        if not errors:
            return f"HTTP {response.status_code}"

        messages = []

        for error in errors[:3]:
            code = error.get("code")
            detail = error.get("detail")
            field = error.get("field")

            part = code or "SQUARE_ERROR"

            if field:
                part += f" [{field}]"

            if detail:
                part += f": {detail}"

            messages.append(part)

        return "; ".join(messages)

    @staticmethod
    def _first_square_error_code(response: httpx.Response) -> str | None:
        """The machine-readable `code` of Square's first reported error, e.g.
        `IDEMPOTENCY_KEY_REUSED`, so callers can branch on it instead of the
        human-readable message text."""
        try:
            data = response.json()
        except ValueError:
            return None

        errors = data.get("errors") or []
        if not errors:
            return None

        return errors[0].get("code")

    @staticmethod
    def _normalize(value: str | None) -> str:
        return " ".join((value or "").lower().strip().split())

    @staticmethod
    def _service_tokens(value: str | None) -> tuple[str, ...]:
        # Voice/menu display labels may append metadata such as
        # " · 60 minutes · 106.96".  That metadata is not part of the service
        # identity and must not poison service/catalog matching.
        raw = (value or "").split(" · ", 1)[0]

        # Strip numbers/number-words only when they belong to a duration phrase
        # (e.g. "60 minutes", "sixty minutes", "2 hrs"). Bare numbers and
        # number-words may be part of the actual service name, such as
        # "HydroLux5" or a speech-to-text result like "HydroLux five".
        text = _DURATION_PHRASE_RE.sub(" ", raw)
        text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
        stop_words = {
            "and",
            "for",
            "the",
            "a",
            "an",
            "with",
            "hour",
            "hours",
            "hr",
            "hrs",
            "minute",
            "minutes",
            "min",
            "mins",
            "service",
            "services",
            "appointment",
            "book",
            "booking",
            "please",
        }
        tokens = [token for token in text.split() if token and token not in stop_words]
        return tuple(tokens)

    @classmethod
    def _service_resolution_candidates(cls, requested: str, configured: dict[str, Any]) -> list[str]:
        requested_tokens = cls._service_tokens(requested)
        matches: list[str] = []

        for candidate_name in configured:
            candidate_tokens = cls._service_tokens(str(candidate_name))
            if not candidate_tokens:
                continue
            if (
                requested_tokens == candidate_tokens
                or set(requested_tokens).issubset(set(candidate_tokens))
                or set(candidate_tokens).issubset(set(requested_tokens))
                or requested_tokens == candidate_tokens[: len(requested_tokens)]
                or candidate_tokens == requested_tokens[: len(candidate_tokens)]
            ):
                matches.append(str(candidate_name))

        return matches

    @classmethod
    def _catalog_search_terms(cls, service_name: str) -> list[str]:
        """Return progressively broader Square catalog text filters.

        Tenant menus often flatten a Square item + variation into one label,
        for example ``HydroLux5 Facial - Face Only`` while Square stores:

            item:      HydroLux5 Facial—The Ultimate Skin Health ...
            variation: Face Only

        Searching Square for the flattened label can return zero items because
        ``Face Only`` is a variation name, not part of the item name.  Try the
        full label first, then the item-like prefix, then a narrow leading token
        as a final discovery fallback.  Candidate validation below still decides
        whether any returned item/variation is actually a match.
        """
        raw = (service_name or "").strip()
        if not raw:
            return []

        # Voice/menu display strings may append metadata such as
        # " · 60 minutes · 106.96".  That metadata is not a Square item name.
        base = raw.split(" · ", 1)[0].strip()

        terms: list[str] = []

        def add(value: str) -> None:
            value = value.strip()
            if value and value not in terms:
                terms.append(value)

        add(base)

        # Our tenant menu convention uses "Item - Variation".  Split only on
        # a spaced separator so hyphens that are genuinely part of a name stay
        # intact.  Also accept typographic dashes for imported menus.
        item_hint = base
        for separator in (" - ", " – ", " — "):
            if separator in item_hint:
                left, right = item_hint.rsplit(separator, 1)
                if left.strip() and right.strip():
                    item_hint = left.strip()
                    add(item_hint)
                    break

        # Final fallback for Square items whose title contains a marketing
        # subtitle after the stable service family name.  Keep this deliberately
        # narrow; local token matching below prevents a broad search from being
        # treated as an automatic match.
        item_tokens = cls._service_tokens(item_hint)
        if item_tokens:
            add(item_tokens[0])

        return terms

    @classmethod
    def _catalog_candidate_match(
        cls,
        service_name: str,
        item_name: str,
        variation_name: str,
    ) -> tuple[bool, bool]:
        """Return ``(matches, exact)`` for one Square item variation.

        This understands flattened local labels such as
        ``HydroLux5 Facial - Face Only`` even when Square stores the item title
        and ``Face Only`` variation separately.
        """
        requested_tokens = cls._service_tokens(service_name)
        item_tokens = cls._service_tokens(item_name)
        variation_tokens = cls._service_tokens(variation_name)

        requested_set = set(requested_tokens)
        item_set = set(item_tokens)
        variation_set = set(variation_tokens)
        combined_set = item_set | variation_set

        if not requested_set:
            return False, False

        exact = (
            requested_set == item_set
            or requested_set == variation_set
            or requested_set == combined_set
        )

        # Strong structured match: the caller/menu named this exact variation,
        # and the remaining service tokens identify the parent Square item.
        variation_named = bool(variation_set) and variation_set.issubset(requested_set)
        requested_item_set = requested_set - variation_set if variation_named else requested_set
        structured = (
            variation_named
            and bool(requested_item_set)
            and requested_item_set.issubset(item_set)
        )

        # If no variation was named, allow a parent-item request to surface all
        # matching variations so the caller can be asked which one they want.
        parent_match = bool(item_set) and (
            requested_set.issubset(item_set)
            or item_set.issubset(requested_set)
        )

        return exact or structured or parent_match, exact or structured

    @staticmethod
    def _square_datetime(value: datetime) -> str:
        if value.tzinfo is None:
            raise BookingProviderError(
                "Square booking datetime must contain timezone information"
            )

        utc_value = value.astimezone(timezone.utc)

        return (
            utc_value.isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _parse_square_datetime(value: str) -> datetime:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).astimezone(timezone.utc)

    def _service_name(self, ctx: BookingContext) -> str:
        value = (
            ctx.service_description
            or ctx.title
            or ""
        ).strip()

        if not value:
            raise BookingProviderError(
                "No appointment service was provided"
            )

        return value

    def _idempotency_key(
        self,
        action: str,
        *values: Any,
    ) -> str:
        """
        Deterministic key.

        Retrying the SAME operation generates the SAME Square key.
        """
        raw = "|".join(
            [
                self.provider,
                self.spa_name,
                action,
                *[str(value or "") for value in values],
            ]
        )

        digest = hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()

        return f"{action}-{digest}"

    # ------------------------------------------------------------------
    # SERVICE RESOLUTION
    # ------------------------------------------------------------------

    async def _resolve_service_variation(
        self,
        ctx: BookingContext,
    ) -> dict[str, Any]:
        """
        Resolve the requested service to a Square CatalogItemVariation.

        Supports optional tenant mappings first.

        Example tenant config:

        {
            "service_variation_ids": {
                "Deep tissue massage": "ABC123"
            }
        }

        If no mapping exists, Square Catalog is searched.
        """

        cache_key = (
            ctx.service_variation_id,
            ctx.service_variation_version,
            self._normalize(self._service_name(ctx)),
        )
        cached = self._service_cache.get(cache_key)
        if cached:
            truth(
                "SERVICE_VERIFIED",
                provider="square",
                service_variation_id=cached.get("id"),
                duration=cached.get("minutes"),
                cache="hit",
            )
            return cached

        def remember(result: dict[str, Any]) -> dict[str, Any]:
            self._service_cache[cache_key] = result
            truth(
                "SERVICE_VERIFIED",
                provider="square",
                service_variation_id=result.get("id"),
                duration=result.get("minutes"),
                cache="store",
            )
            return result

        # Highest priority: an ID already resolved by the tenant's own service
        # menu (`spa_accounts.services[].square_variation_id`), attached to the
        # context by `SpaBookingAdapter._canonicalize_ctx`. This is the only
        # zero-guessing path — no text matching happens at all.
        if ctx.service_variation_id:
            return remember({
                "id": ctx.service_variation_id,
                "version": ctx.service_variation_version,
                "name": ctx.service_description or ctx.title,
            })

        service_name = self._service_name(ctx)
        normalized_name = self._normalize(service_name)

        configured_services = (
            self.square_config.get("service_variation_ids")
            or {}
        )

        if isinstance(configured_services, dict):
            matches = self._service_resolution_candidates(service_name, configured_services)
            if len(matches) == 1:
                canonical_name = matches[0]
                value = configured_services[canonical_name]

                if isinstance(value, str):
                    return {
                        "id": value,
                        "name": canonical_name,
                    }

                if isinstance(value, dict):
                    variation_id = (
                        value.get("id")
                        or value.get("service_variation_id")
                    )

                    if variation_id:
                        return {
                            "id": str(variation_id),
                            "version": value.get("version"),
                            "name": canonical_name,
                        }

            if len(matches) > 1:
                raise ServiceResolutionError(
                    "needs_clarification: service_ambiguous",
                    service_name=service_name,
                )

            if len(matches) == 0 and configured_services:
                raise ServiceResolutionError(
                    "needs_clarification: service_not_recognized",
                    service_name=service_name,
                )

        if not configured_services or not isinstance(configured_services, dict):
            pass

        # Optional single-service configuration.
        configured_id = self.square_config.get(
            "service_variation_id"
        )

        if configured_id:
            return {
                "id": str(configured_id),
                "version": self.square_config.get(
                    "service_variation_version"
                ),
                "name": service_name,
            }

        # Otherwise resolve it from the Square Catalog.  A tenant menu may
        # flatten Square's parent item and variation into one label (for example
        # "HydroLux5 Facial - Face Only").  Square's text_filter searches item
        # text, so progressively broaden discovery until a validated
        # item/variation candidate is found.
        candidates: list[dict[str, Any]] = []
        seen_candidate_ids: set[str] = set()

        for search_term in self._catalog_search_terms(service_name):
            logger.info(
                "Square catalog search: spa=%s requested=%r text_filter=%r",
                self.spa_name,
                service_name,
                search_term,
            )

            data = await self._request(
                "POST",
                "/v2/catalog/search-catalog-items",
                json={
                    "text_filter": search_term,
                    "product_types": [
                        "APPOINTMENTS_SERVICE"
                    ],
                    "enabled_location_ids": [
                        self.location_id
                    ],
                    "archived_state": (
                        "ARCHIVED_STATE_NOT_ARCHIVED"
                    ),
                    "limit": 100,
                },
            )

            for item in data.get("items") or []:
                item_data = item.get("item_data") or {}
                item_name = item_data.get("name") or ""

                for variation in item_data.get("variations") or []:
                    variation_data = variation.get("item_variation_data") or {}

                    if variation_data.get("available_for_booking") is False:
                        continue

                    variation_name = variation_data.get("name") or ""
                    matches, exact = self._catalog_candidate_match(
                        service_name,
                        item_name,
                        variation_name,
                    )
                    if not matches:
                        continue

                    variation_id = variation.get("id")
                    if not variation_id or str(variation_id) in seen_candidate_ids:
                        continue

                    seen_candidate_ids.add(str(variation_id))
                    duration_ms = variation_data.get("service_duration")
                    candidates.append(
                        {
                            "id": variation_id,
                            "version": variation.get("version"),
                            "name": item_name,
                            "variation_name": variation_name,
                            "exact": exact,
                            "minutes": (
                                round(duration_ms / 60000)
                                if isinstance(duration_ms, (int, float)) and duration_ms > 0
                                else None
                            ),
                        }
                    )

            # A validated candidate is enough; do not make broader catalog
            # searches that could introduce unrelated services.
            if candidates:
                break

        candidates = [candidate for candidate in candidates if candidate.get("id")]

        logger.info(
            "Square service candidates for %r: %s",
            service_name,
            [
                (c["name"], c["variation_name"], c["minutes"], c["exact"], c["id"])
                for c in candidates
            ],
        )

        if not candidates:
            raise ServiceResolutionError(
                "needs_clarification: service_not_recognized",
                service_name=service_name,
            )

        pool = candidates

        # 1. An exact name match beats a partial one.
        exact_pool = [c for c in pool if c["exact"]]
        if exact_pool:
            pool = exact_pool

        # 2. Still several: the duration already resolved from the tenant menu
        #    (ctx.end - ctx.start) picks the Square variation of that length.
        if len({c["id"] for c in pool}) > 1:
            start = getattr(ctx, "start", None)
            end = getattr(ctx, "end", None)
            if start is not None and end is not None:
                wanted_minutes = round((end - start).total_seconds() / 60)
                timed = [c for c in pool if c["minutes"] == wanted_minutes]
                if timed:
                    pool = timed

        # 3. Still several: ask, with the real options.
        if len({c["id"] for c in pool}) > 1:
            labels: list[str] = []
            for c in pool:
                if c["variation_name"] in ("", c["name"], "Regular"):
                    label = c["name"]
                else:
                    label = f"{c['name']} - {c['variation_name']}"
                if c["minutes"]:
                    label += f" ({c['minutes']} min)"
                if label not in labels:
                    labels.append(label)
            raise ServiceResolutionError(
                "needs_clarification: service_ambiguous",
                service_name=f"{service_name} | options: {'; '.join(labels[:8])}",
            )

        chosen = pool[0]
        return remember({
            "id": chosen["id"],
            "version": chosen["version"],
            "name": chosen["name"],
            "variation_name": chosen["variation_name"],
            "minutes": chosen.get("minutes"),
        })

    # ------------------------------------------------------------------
    # TEAM MEMBER FILTER
    # ------------------------------------------------------------------

    def _team_member_ids(
        self,
        service_name: str,
    ) -> list[str]:
        """
        Optional tenant configuration:

        {
            "team_member_ids": {
                "Deep tissue massage": [
                    "TEAM_MEMBER_1",
                    "TEAM_MEMBER_2"
                ]
            }
        }

        or:

        {
            "team_member_id": "TEAM_MEMBER_1"
        }
        """

        mapping = (
            self.square_config.get("team_member_ids")
            or {}
        )

        if isinstance(mapping, dict):
            for name, value in mapping.items():
                if (
                    self._normalize(str(name))
                    != self._normalize(service_name)
                ):
                    continue

                if isinstance(value, str):
                    return [value]

                if isinstance(value, list):
                    return [
                        str(item)
                        for item in value
                        if item
                    ]

        default_member = self.square_config.get(
            "team_member_id"
        )

        if default_member:
            return [str(default_member)]

        return []

    async def _active_team_members(self) -> list[dict[str, Any]]:
        cached = getattr(self, "_team_member_cache", None)
        if cached is not None:
            return cached
        data = await self._request(
            "POST",
            "/v2/team-members/search",
            json={
                "query": {
                    "filter": {
                        "location_ids": [self.location_id],
                        "status": "ACTIVE",
                    }
                }
            },
        )
        members = list(data.get("team_members") or [])
        self._team_member_cache = members
        return members

    async def _resolve_preferred_team_member(self, staff_name: str) -> str:
        """Match a caller-named staff member to a bookable Square team member.

        Square's own SearchAvailability has no "by name" filter, so a specific
        request ("can I book with Sarah") has to be resolved to a team_member_id
        first via the team member directory, scoped to this location and only
        active members. If the requested name doesn't match anyone, this raises
        rather than silently booking with someone else — the caller asked for a
        specific person and getting a different one without saying so is worse
        than admitting we couldn't find them.
        """
        members = await self._active_team_members()

        normalized_target = self._normalize(staff_name)
        exact_matches: list[str] = []
        partial_matches: list[str] = []

        for member in members:
            member_id = member.get("id")
            if not member_id:
                continue
            given = str(member.get("given_name") or "")
            family = str(member.get("family_name") or "")
            full_name = f"{given} {family}".strip()
            normalized_given = self._normalize(given)
            normalized_full = self._normalize(full_name)

            if normalized_target in (normalized_given, normalized_full):
                exact_matches.append(str(member_id))
            elif normalized_target and (
                normalized_target in normalized_full
                or normalized_full in normalized_target
            ):
                partial_matches.append(str(member_id))

        matches = exact_matches or partial_matches
        unique = list(dict.fromkeys(matches))

        if len(unique) == 1:
            return unique[0]

        raise TeamMemberResolutionError(
            "requested_staff_not_available: "
            f"no bookable staff member matching {staff_name!r} was found at "
            "this location. Ask the caller to confirm the name, or offer to "
            "book with any available staff member instead.",
            staff_name=staff_name,
        )

    # ------------------------------------------------------------------
    # AVAILABILITY
    # ------------------------------------------------------------------

    async def _verify_pinned_slot(
        self,
        ctx: BookingContext,
    ) -> dict[str, Any] | None:
        """Re-confirm the EXACT previously-selected Square slot.

        Used instead of `_find_exact_availability`'s normal broad search once
        a slot has already been offered to the caller (`ctx.selected_slot`,
        sourced from `BookingDraft.selected_slot`). A fresh, unconstrained
        search at confirm time could legitimately return a *different*
        therapist than the one Square assigned the first time — this instead
        asks Square to confirm that one specific (team member, service
        variation, time) combination is still free, so the recheck can never
        silently substitute someone or something else. If that exact
        combination is no longer free, this returns None like any other
        unavailable slot; it does not fall back to a broader search.
        """
        slot = ctx.selected_slot or {}
        team_member_id = slot.get("team_member_id")
        service_variation_id = slot.get("service_variation_id")
        if not team_member_id or not service_variation_id:
            # Nothing usable was pinned; behave as if nothing were selected.
            return None

        target_start = ctx.start.astimezone(timezone.utc)
        search_end = target_start + timedelta(hours=24)
        payload = {
            "query": {
                "filter": {
                    "start_at_range": {
                        "start_at": self._square_datetime(target_start),
                        "end_at": self._square_datetime(search_end),
                    },
                    "location_id": self.location_id,
                    "segment_filters": [
                        {
                            "service_variation_id": service_variation_id,
                            "team_member_id_filter": {"any": [team_member_id]},
                        }
                    ],
                }
            }
        }

        logger.info(
            "Square availability recheck (pinned slot): spa=%s "
            "team_member_id=%s service_variation_id=%s location_id=%s "
            "range_start_utc=%s range_end_utc=%s",
            self.spa_name,
            team_member_id,
            service_variation_id,
            self.location_id,
            payload["query"]["filter"]["start_at_range"]["start_at"],
            payload["query"]["filter"]["start_at_range"]["end_at"],
        )

        data = await self._request(
            "POST",
            "/v2/bookings/availability/search",
            json=payload,
        )
        availabilities = data.get("availabilities") or []
        logger.info("availability_count=%s", len(availabilities))
        logger.info(
            "Square availability response (pinned): spa=%s slot_count=%d starts=%s",
            self.spa_name,
            len(availabilities),
            [a.get("start_at") for a in availabilities[:10]],
        )

        for availability in availabilities:
            raw_start = availability.get("start_at")
            if not raw_start:
                continue
            if self._parse_square_datetime(raw_start) != target_start:
                continue
            if availability.get("location_id") != self.location_id:
                continue
            segments = availability.get("appointment_segments") or []
            matching = [
                segment
                for segment in segments
                if segment.get("service_variation_id") == service_variation_id
                and str(segment.get("team_member_id")) == str(team_member_id)
            ]
            if matching:
                return availability
        return None

    def _authoritative_slot(self, availability: dict[str, Any]) -> dict[str, Any]:
        """Provider-agnostic authoritative slot summary from a real Square
        SearchAvailability result, for persisting on the booking draft.

        Never computed or guessed — every field here comes directly from
        Square's own response. Takes the first appointment segment, which
        matches how `create_booking` already builds its payload; this
        booking engine's data model (one title/start/end per `Appointment`)
        does not represent multi-segment bookings, so there is nothing to
        gain by summarising more than one.
        """
        segments = availability.get("appointment_segments") or []
        segment = segments[0] if segments else {}
        return {
            "start": availability.get("start_at"),
            "location_id": availability.get("location_id") or self.location_id,
            "team_member_id": segment.get("team_member_id"),
            "service_variation_id": segment.get("service_variation_id"),
            "service_variation_version": segment.get("service_variation_version"),
            "duration_minutes": segment.get("duration_minutes"),
            "provider": self.provider,
        }

    async def _search_square_availabilities(
        self,
        ctx: BookingContext,
        range_start: datetime,
        range_end: datetime,
    ) -> tuple[list[dict[str, Any]], str, list[str]]:
        """One Square SearchAvailability call for a service/staff/location range."""
        service = await self._resolve_service_variation(ctx)
        await self._location()
        service_variation_id = str(service["id"])
        service_name = self._service_name(ctx)
        if ctx.provider_id:
            team_members = [str(ctx.provider_id)]
        elif ctx.preferred_staff:
            team_members = [
                await self._resolve_preferred_team_member(ctx.preferred_staff)
            ]
        elif ctx.allowed_staff:
            team_members = []
            for name in ctx.allowed_staff:
                try:
                    team_members.append(await self._resolve_preferred_team_member(name))
                except TeamMemberResolutionError:
                    logger.info(
                        "Dashboard staff %r is not a Square team member at %s",
                        name,
                        self.spa_name,
                    )
            team_members = list(dict.fromkeys(team_members))
            if not team_members:
                raise TeamMemberResolutionError(
                    "requested_staff_not_available: the dashboard assigns this "
                    "service to staff who are not bookable in Square. Do not "
                    "offer a different provider.",
                    staff_name=", ".join(ctx.allowed_staff),
                )
        else:
            team_members = self._team_member_ids(service_name)

        segment_filter: dict[str, Any] = {
            "service_variation_id": service_variation_id,
        }
        if team_members:
            segment_filter["team_member_id_filter"] = {"any": team_members}

        payload = {
            "query": {
                "filter": {
                    "start_at_range": {
                        "start_at": self._square_datetime(range_start.astimezone(timezone.utc)),
                        "end_at": self._square_datetime(range_end.astimezone(timezone.utc)),
                    },
                    "location_id": self.location_id,
                    "segment_filters": [segment_filter],
                }
            }
        }
        request_id = getattr(self, "_availability_request_id", None) or uuid.uuid4().hex[:12]
        source = getattr(self, "_availability_source", "exact_check")
        truth("AVAILABILITY_CHECK_STARTED", request_id=request_id, provider="square", source=source)
        if source == "revalidation":
            truth("AVAILABILITY_REVALIDATION", request_id=request_id, provider="square")
        truth(
            "AVAILABILITY_REQUEST",
            request_id=request_id,
            provider="square",
            source=source,
            location_id=self.location_id,
            service_variation_id=service_variation_id,
            requested_start=self._square_datetime(range_start.astimezone(timezone.utc)),
            requested_end=self._square_datetime(range_end.astimezone(timezone.utc)),
            staff=ctx.preferred_staff or "ANY",
        )
        started = time.perf_counter()
        data = await self._request(
            "POST",
            "/v2/bookings/availability/search",
            json=payload,
        )
        self._last_availability_latency_ms = int((time.perf_counter() - started) * 1000)
        availabilities = data.get("availabilities") or []
        logger.info(
            "Square availability response: spa=%s slot_count=%d",
            self.spa_name,
            len(availabilities),
        )
        return availabilities, service_variation_id, team_members

    def _slot_matches_request(
        self,
        availability: dict[str, Any],
        *,
        target_start: datetime | None,
        service_variation_id: str,
        team_members: list[str],
        expected_duration: int,
        service_name: str,
    ) -> bool:
        raw_start = availability.get("start_at")
        if not raw_start:
            return False
        available_start = self._parse_square_datetime(raw_start)
        if target_start is not None and available_start != target_start:
            return False
        if availability.get("location_id") != self.location_id:
            return False
        segments = availability.get("appointment_segments") or []
        if not segments:
            return False
        matching_segments = [
            segment
            for segment in segments
            if segment.get("service_variation_id") == service_variation_id
        ]
        if not matching_segments:
            return False
        total_duration = sum(int(segment.get("duration_minutes") or 0) for segment in segments)
        if expected_duration > 0 and total_duration > 0 and total_duration != expected_duration:
            logger.warning(
                "Square duration overrides local value spa=%s service=%s local=%s square=%s",
                self.spa_name,
                service_name,
                expected_duration,
                total_duration,
            )
        if team_members:
            segment_team_ids = {
                str(segment.get("team_member_id"))
                for segment in matching_segments
                if segment.get("team_member_id")
            }
            if not (segment_team_ids & set(team_members)):
                return False
        return True

    async def _find_exact_availability(
        self,
        ctx: BookingContext,
    ) -> dict[str, Any] | None:
        if ctx.selected_slot:
            self._last_range_slots = []
            return await self._verify_pinned_slot(ctx)

        target_start = ctx.start.astimezone(timezone.utc)
        search_end = target_start + timedelta(hours=24)
        availabilities, service_variation_id, team_members = (
            await self._search_square_availabilities(ctx, target_start, search_end)
        )
        expected_duration = int((ctx.end - ctx.start).total_seconds() / 60)
        service_name = self._service_name(ctx)

        matching: list[dict[str, Any]] = []
        exact: dict[str, Any] | None = None
        for availability in availabilities:
            if not self._slot_matches_request(
                availability,
                target_start=None,
                service_variation_id=service_variation_id,
                team_members=team_members,
                expected_duration=expected_duration,
                service_name=service_name,
            ):
                continue
            matching.append(availability)
            available_start = self._parse_square_datetime(availability.get("start_at"))
            if available_start == target_start:
                exact = availability

        self._last_range_slots = [
            self._authoritative_slot(item)
            for item in matching
            if item is not exact
        ]
        later = []
        for slot in self._last_range_slots:
            raw = slot.get("start")
            if not raw:
                continue
            try:
                start_at = self._parse_square_datetime(raw)
            except Exception:
                continue
            if start_at > target_start:
                later.append(slot)
            if len(later) == 3:
                break
        self._last_range_slots = later
        return exact

    async def list_openings(
        self,
        ctx: BookingContext,
        range_start: datetime,
        range_end: datetime,
    ) -> list[dict[str, Any]]:
        self._validate_config()
        availabilities, service_variation_id, team_members = (
            await self._search_square_availabilities(ctx, range_start, range_end)
        )
        expected_duration = int((ctx.end - ctx.start).total_seconds() / 60)
        service_name = self._service_name(ctx)
        slots: list[dict[str, Any]] = []
        for availability in availabilities:
            if not self._slot_matches_request(
                availability,
                target_start=None,
                service_variation_id=service_variation_id,
                team_members=team_members,
                expected_duration=expected_duration,
                service_name=service_name,
            ):
                continue
            slots.append(self._authoritative_slot(availability))
        slots.sort(key=lambda item: str(item.get("start") or ""))
        return slots

    def _local_day_bounds(self, moment: datetime) -> tuple[datetime, datetime]:
        tz_name = self.timezone_name
        tz = ZoneInfo(tz_name) if tz_name else (moment.tzinfo or timezone.utc)
        local = moment.astimezone(tz)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    @staticmethod
    def _nearest_slots(
        slots: list[dict[str, Any]],
        target: datetime,
        *,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        ranked: list[tuple[float, dict[str, Any]]] = []
        for slot in slots:
            raw = slot.get("start")
            if not raw:
                continue
            try:
                start_at = SquareAdapter._parse_square_datetime(str(raw))
            except Exception:
                continue
            if start_at == target:
                continue
            ranked.append((abs((start_at - target).total_seconds()), slot))
        # Full local day, including earlier times. Closest real Square slot first.
        ranked.sort(key=lambda item: (item[0], str(item[1].get("start") or "")))
        return [slot for _distance, slot in ranked[:limit]]

    async def check_availability(
        self,
        ctx: BookingContext,
    ) -> AvailabilityVerdict:
        self._validate_config()
        self._availability_source = "revalidation" if ctx.selected_slot else "exact_check"
        self._availability_request_id = uuid.uuid4().hex[:12]

        try:
            availability = await self._find_exact_availability(ctx)
        except ServiceResolutionError as exc:
            return AvailabilityVerdict.no(
                f"{exc.reason}: {exc.service_name or 'service_not_recognized'}"
            )
        except TeamMemberResolutionError as exc:
            return AvailabilityVerdict.no(exc.reason)

        # A pinned recheck must not be replaced with a different slot. An exact
        # miss searches the whole requested calendar day and ranks those Square
        # slots by closeness, including times earlier than the request.
        if not availability and not ctx.selected_slot:
            day_start, day_end = self._local_day_bounds(ctx.start)
            self._availability_source = "alternative_search"
            openings = await self.list_openings(ctx, day_start, day_end)
            target = ctx.start.astimezone(timezone.utc)
            exact = next(
                (
                    slot for slot in openings
                    if self._parse_square_datetime(str(slot.get("start"))) == target
                ),
                None,
            )
            if exact:
                truth(
                    "AVAILABILITY_RESULT",
                    available=True,
                    provider="square",
                    location_id=exact.get("location_id"),
                    provider_slot=exact.get("start"),
                    request_id=self._availability_request_id,
                    slot_count=1,
                    verified_slots=1,
                    source="alternative_search",
                )
                return AvailabilityVerdict.ok(slot=exact)
            self._last_range_slots = self._nearest_slots(openings, target)

        if not availability:
            alts = tuple(self._last_range_slots)
            truth(
                "AVAILABILITY_RESULT",
                available=False,
                provider="square",
                location_id=self.location_id,
                request_id=getattr(self, "_availability_request_id", None),
                slot_count=0,
                verified_slots=len(alts),
                latency_ms=getattr(self, "_last_availability_latency_ms", None),
                source=getattr(self, "_availability_source", "exact_check"),
            )
            return AvailabilityVerdict.no(
                "The requested appointment time is not available in Square",
                alternatives=alts,
            )

        slot = self._authoritative_slot(availability)
        truth(
            "AVAILABILITY_RESULT",
            available=True,
            provider="square",
            location_id=slot.get("location_id"),
            provider_slot=slot.get("start"),
            team_member_id=slot.get("team_member_id"),
            service_variation_id=slot.get("service_variation_id"),
            request_id=getattr(self, "_availability_request_id", None),
            slot_count=1,
            verified_slots=1,
            latency_ms=getattr(self, "_last_availability_latency_ms", None),
            source=getattr(self, "_availability_source", "exact_check"),
        )
        return AvailabilityVerdict.ok(slot=slot)

    # ------------------------------------------------------------------
    # CUSTOMER
    # ------------------------------------------------------------------

    def _phone_for_customer(self, phone: str | None) -> str | None:
        return canonical_customer_phone(phone, timezone_name=self.timezone_name)

    @staticmethod
    def _provider_customer(customer: dict[str, Any]) -> ProviderCustomer | None:
        customer_id = customer.get("id")
        if not customer_id:
            return None
        given = (customer.get("given_name") or "").strip() or None
        family = (customer.get("family_name") or "").strip() or None
        display = " ".join(part for part in (given, family) if part) or None
        return ProviderCustomer(
            external_customer_id=str(customer_id),
            display_name=display,
            given_name=given,
            family_name=family,
            phone=(customer.get("phone_number") or None),
            email=(customer.get("email_address") or None),
        )

    async def _search_customers(self, phone: str) -> list[dict[str, Any]]:
        """Every Square customer with this exact phone. Never picks one."""
        search_data = await self._request(
            "POST",
            "/v2/customers/search",
            json={
                "limit": 10,
                "query": {
                    "filter": {
                        "phone_number": {
                            "exact": phone
                        }
                    }
                },
            },
        )
        customers = search_data.get("customers") or []
        return [customer for customer in customers if isinstance(customer, dict)]

    def _remember(self, customer: ProviderCustomer, resolution: str) -> str:
        self._resolved_customer = customer
        self._customer_resolution = resolution
        return customer.external_customer_id

    async def find_customers_by_phone(self, phone: str) -> CustomerLookupResult:
        canonical = self._phone_for_customer(phone)
        if not canonical:
            return CustomerLookupResult(
                outcome=CustomerLookupOutcome.SEARCH_FAILED,
                detail="invalid_phone",
            )
        try:
            customers = await self._search_customers(canonical)
        except BookingProviderError as exc:
            return CustomerLookupResult(
                outcome=CustomerLookupOutcome.SEARCH_FAILED,
                detail=str(exc),
            )
        parsed = tuple(
            customer
            for customer in (self._provider_customer(row) for row in customers)
            if customer is not None
        )
        if not parsed:
            return CustomerLookupResult(outcome=CustomerLookupOutcome.NO_MATCH)
        if len(parsed) == 1:
            return CustomerLookupResult(
                outcome=CustomerLookupOutcome.MATCHED,
                customer=parsed[0],
                candidates=parsed,
            )
        return CustomerLookupResult(
            outcome=CustomerLookupOutcome.AMBIGUOUS,
            candidates=parsed,
        )

    async def get_customer(self, external_customer_id: str) -> CustomerLookupResult:
        if not external_customer_id:
            return CustomerLookupResult(outcome=CustomerLookupOutcome.NO_MATCH)
        try:
            data = await self._request("GET", f"/v2/customers/{external_customer_id}")
        except BookingProviderError as exc:
            return CustomerLookupResult(
                outcome=CustomerLookupOutcome.SEARCH_FAILED,
                detail=str(exc),
            )
        parsed = self._provider_customer((data or {}).get("customer") or {})
        if parsed is None:
            return CustomerLookupResult(outcome=CustomerLookupOutcome.NO_MATCH)
        return CustomerLookupResult(
            outcome=CustomerLookupOutcome.MATCHED,
            customer=parsed,
            candidates=(parsed,),
        )

    async def update_customer(
        self, external_customer_id: str, ctx: BookingContext
    ) -> CustomerLookupResult:
        """Write a provider profile. Booking resolution must not call this.

        Reserved for a future explicit correction, after the caller has asked
        to change their own profile. Saying a name during booking is not that.
        """
        if not external_customer_id:
            return CustomerLookupResult(outcome=CustomerLookupOutcome.NO_MATCH)
        current = await self.get_customer(external_customer_id)
        if current.outcome != CustomerLookupOutcome.MATCHED or current.customer is None:
            return current
        profile_name = caller_profile_name(
            caller_name=ctx.caller_name,
            guest_name=ctx.guest_name,
            customer_name=ctx.customer_name,
        )
        parts = (profile_name or "").split(maxsplit=1)
        update_payload: dict[str, Any] = {}
        if parts:
            update_payload["given_name"] = parts[0]
        if len(parts) > 1:
            update_payload["family_name"] = parts[1]
        if ctx.customer_email:
            update_payload["email_address"] = ctx.customer_email
        if not update_payload:
            return current
        try:
            data = await self._request(
                "PUT",
                f"/v2/customers/{external_customer_id}",
                json=update_payload,
            )
        except BookingProviderError as exc:
            return CustomerLookupResult(
                outcome=CustomerLookupOutcome.SEARCH_FAILED,
                detail=str(exc),
            )
        parsed = self._provider_customer((data or {}).get("customer") or {})
        if parsed is None:
            parsed = ProviderCustomer(
                external_customer_id=external_customer_id,
                display_name=profile_name,
                given_name=parts[0] if parts else None,
                family_name=parts[1] if len(parts) > 1 else None,
                email=ctx.customer_email,
            )
        return CustomerLookupResult(
            outcome=CustomerLookupOutcome.MATCHED,
            customer=parsed,
        )

    async def _insert_customer(self, ctx: BookingContext, phone: str) -> ProviderCustomer:
        """Create one Square customer after a confirmed zero-match search.

        The guest name is never written onto this profile. A missing caller
        name is left blank rather than stored as a placeholder.
        """
        self._recovered_existing = False
        profile_name = caller_profile_name(
            caller_name=ctx.caller_name,
            guest_name=ctx.guest_name,
            customer_name=ctx.customer_name,
        )
        guest = " ".join((ctx.guest_name or "").split())
        if guest and not profile_name:
            raise BookingProviderError(
                "This appointment is for a guest, and the caller's own name "
                "is not known. Do not create a provider customer from the "
                "guest name or the caller's phone.",
                code="GUEST_UNRESOLVED",
            )
        parts = (profile_name or "").split()
        given_name = parts[0] if parts else ""
        family_name = " ".join(parts[1:]) if len(parts) > 1 else ""
        payload: dict[str, Any] = {
            "idempotency_key": self._idempotency_key(
                "customer",
                phone,
                given_name,
                family_name,
                ctx.customer_email or "",
            ),
            "phone_number": phone,
        }
        if given_name:
            payload["given_name"] = given_name
        if family_name:
            payload["family_name"] = family_name
        if ctx.customer_email:
            payload["email_address"] = ctx.customer_email

        try:
            created = await self._request("POST", "/v2/customers", json=payload)
        except BookingProviderError as exc:
            if exc.code != "IDEMPOTENCY_KEY_REUSED":
                raise
            # An earlier create for this same caller likely succeeded, and
            # the search index had not caught up. Re-search. Do not retry
            # the create, and do not rewrite whatever customer comes back.
            for attempt in range(3):
                await asyncio.sleep(0.3 * (attempt + 1))
                try:
                    recovered = await self.find_customers_by_phone(phone)
                except BookingProviderError:
                    continue
                if recovered.outcome == CustomerLookupOutcome.MATCHED and recovered.customer:
                    logger.warning(
                        "Square customer create idempotency conflict spa=%s "
                        "phone_last4=%s recovered_customer=%s attempt=%s",
                        self.spa_name,
                        phone[-4:],
                        recovered.customer.external_customer_id,
                        attempt + 1,
                    )
                    self._recovered_existing = True
                    return recovered.customer
                if recovered.outcome == CustomerLookupOutcome.AMBIGUOUS:
                    raise BookingProviderError(
                        ambiguous_customer_message(recovered.candidates),
                        code="AMBIGUOUS_CUSTOMER",
                    ) from exc
            raise BookingProviderError(
                "Square rejected the customer-create request as a reused "
                "idempotency key, and no matching customer could be found "
                "by phone. The prior attempt's outcome is unknown.",
                code="CUSTOMER_IDEMPOTENCY_CONFLICT",
            ) from exc

        customer = self._provider_customer((created or {}).get("customer") or {})
        if customer is None:
            raise BookingProviderError("Square did not return a customer id")
        if not customer.phone:
            customer = ProviderCustomer(
                external_customer_id=customer.external_customer_id,
                display_name=customer.display_name or profile_name or None,
                given_name=customer.given_name or (given_name or None),
                family_name=customer.family_name or (family_name or None),
                phone=phone,
                email=customer.email or ctx.customer_email,
            )
        return customer

    async def create_customer(self, ctx: BookingContext) -> CustomerLookupResult:
        """Create only after this provider's own search returns zero matches."""
        phone = self._phone_for_customer(ctx.customer_phone)
        if not phone:
            return CustomerLookupResult(
                outcome=CustomerLookupOutcome.SEARCH_FAILED,
                detail="invalid_phone",
            )
        existing = await self.find_customers_by_phone(phone)
        if existing.outcome != CustomerLookupOutcome.NO_MATCH:
            return existing
        try:
            created = await self._insert_customer(ctx, phone)
        except BookingProviderError as exc:
            if exc.code == "AMBIGUOUS_CUSTOMER":
                return CustomerLookupResult(
                    outcome=CustomerLookupOutcome.AMBIGUOUS,
                    detail=str(exc),
                )
            if exc.code == "GUEST_UNRESOLVED":
                return CustomerLookupResult(
                    outcome=CustomerLookupOutcome.SEARCH_FAILED,
                    detail="guest_unresolved",
                )
            return CustomerLookupResult(
                outcome=CustomerLookupOutcome.SEARCH_FAILED,
                detail=str(exc),
            )
        return CustomerLookupResult(
            outcome=CustomerLookupOutcome.MATCHED,
            customer=created,
            candidates=(created,),
        )

    async def _get_or_create_customer(self, ctx: BookingContext) -> str:
        """Resolve the caller's Square customer without changing an existing one.

        One confident match is reused as-is. Several matches stay unresolved
        unless the caller's own name identifies exactly one. A new customer is
        created only after a search that succeeded with zero matches.
        """
        if ctx.external_customer_id:
            return self._remember(
                ProviderCustomer(
                    external_customer_id=ctx.external_customer_id,
                    display_name=caller_profile_name(
                        caller_name=ctx.caller_name,
                        guest_name=ctx.guest_name,
                        customer_name=ctx.customer_name,
                    ),
                    phone=self._phone_for_customer(ctx.customer_phone),
                    email=ctx.customer_email,
                ),
                "reused",
            )

        phone = self._phone_for_customer(ctx.customer_phone)
        if not phone:
            raise BookingProviderError(
                "The caller ID is missing, withheld, or not a usable phone "
                "number. Ask for a phone number. Do not store the withheld "
                "caller ID as a customer.",
                code="INVALID_CALLER_ID",
            )

        lookup = await self.find_customers_by_phone(phone)
        if lookup.outcome == CustomerLookupOutcome.SEARCH_FAILED:
            raise BookingProviderError(
                "Square customer search failed. No customer was created.",
                code="CUSTOMER_SEARCH_FAILED",
            )
        if lookup.outcome == CustomerLookupOutcome.AMBIGUOUS:
            chosen = unique_customer_by_name(
                lookup.candidates,
                caller_profile_name(
                    caller_name=ctx.caller_name,
                    guest_name=ctx.guest_name,
                    customer_name=ctx.customer_name,
                ),
            )
            if chosen is None:
                raise BookingProviderError(
                    ambiguous_customer_message(lookup.candidates),
                    code="AMBIGUOUS_CUSTOMER",
                )
            return self._remember(chosen, "reused")
        if lookup.outcome == CustomerLookupOutcome.MATCHED and lookup.customer:
            return self._remember(lookup.customer, "reused")
        if lookup.outcome != CustomerLookupOutcome.NO_MATCH:
            raise BookingProviderError(
                "Square customer lookup did not complete. No customer was created.",
                code="CUSTOMER_SEARCH_FAILED",
            )

        created = await self._insert_customer(ctx, phone)
        return self._remember(
            created,
            "reused" if self._recovered_existing else "created",
        )

    # ------------------------------------------------------------------
    # SAVED CARDS
    # ------------------------------------------------------------------

    def _card_today(self) -> datetime:
        tz_name = self.timezone_name or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        return datetime.now(tz)

    @classmethod
    def _card_is_usable(cls, card: dict[str, Any], *, today: datetime) -> bool:
        """Enabled and unexpired through the end of its expiration month.

        Disabled, expired, or incomplete cards do not count. This does not
        delete or change the card at Square.
        """
        if not isinstance(card, dict) or card.get("enabled") is not True:
            return False
        if not card.get("id"):
            return False
        try:
            month = int(card.get("exp_month"))
            year = int(card.get("exp_year"))
        except (TypeError, ValueError):
            return False
        if not 1 <= month <= 12 or year < 2000:
            return False
        if year > today.year:
            return True
        return year == today.year and month >= today.month

    async def list_saved_payment_methods(self, external_customer_id: str) -> SavedPaymentLookup:
        """GET /v2/cards for this spa's resolved customer. Does not create or charge."""
        customer_id = (external_customer_id or "").strip()
        if not customer_id:
            return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.FAILED)
        examined = 0
        usable = 0
        cursor: str | None = None
        seen_cursors: set[str] = set()
        today = self._card_today()
        # A usable card ends the search. A later page error is not "no card".
        # If Square keeps returning a cursor past this bound, the result is a
        # failed lookup rather than a claim that no usable card exists.
        for _page in range(50):
            params: dict[str, Any] = {
                "customer_id": customer_id,
                "include_disabled": "true",
            }
            if cursor:
                params["cursor"] = cursor
            try:
                data = await self._request("GET", "/v2/cards", params=params)
            except BookingProviderError:
                logger.warning(
                    "Square saved-card lookup failed spa=%s",
                    self.spa_name,
                )
                return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.FAILED)
            if not isinstance(data, dict):
                return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.FAILED)
            cards = data.get("cards", [])
            if cards is None:
                cards = []
            if not isinstance(cards, list):
                return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.FAILED)
            for card in cards:
                examined += 1
                if self._card_is_usable(card, today=today):
                    logger.info(
                        "Square saved-card lookup spa=%s examined=%s usable=1",
                        self.spa_name,
                        examined,
                    )
                    return SavedPaymentLookup(
                        outcome=SavedPaymentLookupOutcome.USABLE,
                        usable_count=1,
                        examined_count=examined,
                    )
            next_cursor = data.get("cursor") or None
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.FAILED)
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.FAILED)
        outcome = SavedPaymentLookupOutcome.NO_USABLE_METHOD
        logger.info(
            "Square saved-card lookup spa=%s examined=%s usable=%s",
            self.spa_name,
            examined,
            usable,
        )
        return SavedPaymentLookup(
            outcome=outcome,
            usable_count=usable,
            examined_count=examined,
        )

    async def has_usable_saved_payment_method(
        self, external_customer_id: str
    ) -> SavedPaymentLookup:
        return await self.list_saved_payment_methods(external_customer_id)

    async def save_card_on_file(
        self,
        *,
        external_customer_id: str,
        source_id: str,
        idempotency_key: str,
        verification_token: str | None = None,
    ) -> SaveCardResult:
        """POST /v2/cards for the token-bound customer. Does not take a payment."""
        customer_id = (external_customer_id or "").strip()
        source = (source_id or "").strip()
        key = (idempotency_key or "").strip()[:45]
        if not customer_id or not source or not key or _raw_card_material(source):
            return SaveCardResult(outcome=SaveCardOutcome.REJECTED, error_code="INVALID_SOURCE")
        verification = (verification_token or "").strip() or None
        if verification and _raw_card_material(verification):
            return SaveCardResult(outcome=SaveCardOutcome.REJECTED, error_code="INVALID_SOURCE")
        payload: dict[str, Any] = {
            "idempotency_key": key,
            "source_id": source,
            "card": {"customer_id": customer_id},
        }
        if verification:
            payload["verification_token"] = verification

        async def _once() -> dict[str, Any]:
            data = await self._request("POST", "/v2/cards", json=payload)
            return data if isinstance(data, dict) else {}

        try:
            data = await _once()
        except BookingProviderError as exc:
            if not exc.retryable:
                return SaveCardResult(outcome=SaveCardOutcome.REJECTED, error_code=exc.code)
            try:
                data = await _once()
            except BookingProviderError as retry_exc:
                outcome = (
                    SaveCardOutcome.AMBIGUOUS
                    if retry_exc.retryable
                    else SaveCardOutcome.REJECTED
                )
                return SaveCardResult(outcome=outcome, error_code=retry_exc.code)
        card = data.get("card")
        if (
            not isinstance(card, dict)
            or card.get("customer_id") != customer_id
            or not card.get("id")
        ):
            logger.warning("Square card save customer mismatch spa=%s", self.spa_name)
            return SaveCardResult(
                outcome=SaveCardOutcome.REJECTED,
                error_code="CUSTOMER_MISMATCH",
            )
        logger.info("Square card saved on file spa=%s", self.spa_name)
        return SaveCardResult(outcome=SaveCardOutcome.SAVED)

    # ------------------------------------------------------------------
    # CREATE BOOKING
    # ------------------------------------------------------------------

    async def create_booking(
        self,
        ctx: BookingContext,
    ) -> ExternalBooking:
        self._validate_config()
        await self._location()

        if ctx.selected_slot:
            slot_location = str(ctx.selected_slot.get("location_id") or "")
            if slot_location and slot_location != self.location_id:
                raise BookingProviderError(
                    "Selected slot is for a different Square location than this spa"
                )

        truth("BOOKING_CREATE_STARTED", provider="square", location_id=self.location_id)
        truth(
            "BOOKING_CREATE_START",
            provider="square",
            location_id=self.location_id,
            start_at=ctx.start.isoformat(),
        )

        # IMPORTANT:
        # Re-check Square immediately before booking.
        try:
            availability = (
                await self._find_exact_availability(
                    ctx
                )
            )
        except (ServiceResolutionError, TeamMemberResolutionError) as exc:
            # Surfaced as a provider error (not a raw ValueError) so the
            # booking engine's generic exception handling maps it to a
            # sensible spoken message instead of "an internal error occurred".
            raise BookingProviderError(exc.reason) from exc

        if not availability:
            raise BookingProviderError(
                "The requested appointment is no "
                "longer available in Square"
            )

        customer_id = (
            await self._get_or_create_customer(
                ctx
            )
        )

        if ctx.selected_slot:
            # The caller already heard this exact therapist/service/time read
            # back to them. `_verify_pinned_slot` above only proved that
            # SPECIFIC combination is still free — build the write from the
            # pinned values themselves, not from whatever Square's recheck
            # response happens to contain, so nothing can drift between what
            # was offered and what gets booked.
            pinned = ctx.selected_slot
            team_member_id = pinned.get("team_member_id")
            service_variation_id = pinned.get("service_variation_id")
            service_variation_version = pinned.get("service_variation_version")
            start_at = pinned.get("start")

            if (
                not team_member_id
                or not service_variation_id
                or service_variation_version is None
                or not start_at
            ):
                raise BookingProviderError(
                    "Selected Square slot is missing required booking fields"
                )

            segment_payload = {
                "team_member_id": team_member_id,
                "service_variation_id": service_variation_id,
                "service_variation_version": service_variation_version,
            }
            duration = pinned.get("duration_minutes")
            if duration is not None:
                segment_payload["duration_minutes"] = int(duration)
            segments = [segment_payload]
        else:
            segments = []

            for segment in (
                availability.get(
                    "appointment_segments"
                )
                or []
            ):
                team_member_id = segment.get(
                    "team_member_id"
                )

                service_variation_id = (
                    segment.get(
                        "service_variation_id"
                    )
                )

                service_variation_version = (
                    segment.get(
                        "service_variation_version"
                    )
                )

                if (
                    not team_member_id
                    or not service_variation_id
                    or service_variation_version
                    is None
                ):
                    raise BookingProviderError(
                        "Square availability returned "
                        "an incomplete appointment segment"
                    )

                segment_payload = {
                    "team_member_id": (
                        team_member_id
                    ),
                    "service_variation_id": (
                        service_variation_id
                    ),
                    "service_variation_version": (
                        service_variation_version
                    ),
                }

                duration = segment.get(
                    "duration_minutes"
                )

                if duration is not None:
                    segment_payload[
                        "duration_minutes"
                    ] = int(duration)

                segments.append(
                    segment_payload
                )

            if not segments:
                raise BookingProviderError(
                    "Square returned no appointment "
                    "segments for this slot"
                )

            start_at = availability.get(
                "start_at"
            )

            if not start_at:
                raise BookingProviderError(
                    "Square availability has no start time"
                )

        service_ids = ",".join(
            str(
                segment[
                    "service_variation_id"
                ]
            )
            for segment in segments
        )

        idempotency_key = (
            self._idempotency_key(
                "booking",
                ctx.customer_phone,
                start_at,
                self.location_id,
                service_ids,
            )
        )

        payload = {
            "idempotency_key": (
                idempotency_key
            ),
            "booking": {
                "location_id": (
                    self.location_id
                ),
                "start_at": start_at,
                "customer_id": customer_id,
                "appointment_segments": (
                    segments
                ),
            },
        }
        resolved_for_note = self._resolved_customer
        customer_note, mismatch_noted = _square_customer_note(
            ctx,
            on_file=resolved_for_note.display_name if resolved_for_note else None,
            resolution=self._customer_resolution,
        )
        if customer_note:
            payload["booking"]["customer_note"] = customer_note
        if mismatch_noted:
            truth(
                "BOOKING_CALLER_NAME_MISMATCH_NOTE_ADDED",
                provider="square",
            )

        logger.info(
            "Square create_booking: spa=%s start_at=%s location_id=%s "
            "customer_id=%s team_member_ids=%s service_variation_ids=%s",
            self.spa_name,
            start_at,
            self.location_id,
            customer_id,
            [seg.get("team_member_id") for seg in segments],
            [seg.get("service_variation_id") for seg in segments],
        )

        data = await self._request(
            "POST",
            "/v2/bookings",
            json=payload,
        )

        booking = (
            data.get("booking")
            or {}
        )

        booking_id = booking.get("id")

        if not booking_id:
            raise BookingProviderError(
                "Square did not return a booking id"
            )

        # Square replays the original CreateBooking response for the same
        # idempotency key, including after that appointment was cancelled.
        # An id alone is not an active booking the caller can attend.
        square_status = str(booking.get("status") or "").upper()
        if square_status and square_status not in {"ACCEPTED", "PENDING"}:
            truth(
                "BOOKING_CREATE_REJECTED",
                provider="square",
                external_booking_id=booking_id,
                square_status=square_status,
                reason="inactive_booking",
            )
            raise BookingProviderError(
                "Square did not create an active booking.",
                code="INACTIVE_BOOKING",
            )

        logger.info("booking_id=%s", booking_id)
        truth(
            "BOOKING_CREATE_SUCCESS",
            provider="square",
            external_booking_id=booking_id,
            location_id=self.location_id,
        )
        truth(
            "BOOKING_PROVIDER_CREATE_SUCCESS",
            provider="square",
            external_booking_id=booking_id,
        )

        resolved = self._resolved_customer
        return ExternalBooking(
            provider=self.provider,
            external_id=str(booking_id),
            external_customer_id=customer_id,
            customer_display_name=resolved.display_name if resolved else None,
            customer_email=resolved.email if resolved else None,
            customer_phone=resolved.phone if resolved else None,
            customer_resolution=self._customer_resolution,
        )

    # ------------------------------------------------------------------
    # RETRIEVE BOOKING
    # ------------------------------------------------------------------

    async def _retrieve_booking(
        self,
        booking_id: str,
    ) -> dict[str, Any]:
        data = await self._request(
            "GET",
            f"/v2/bookings/{booking_id}",
        )

        square_booking = (
            data.get("booking")
            or {}
        )

        if not square_booking.get("id"):
            raise BookingProviderError(
                "Square booking could not be retrieved"
            )

        return square_booking

    # ------------------------------------------------------------------
    # RESCHEDULE
    # ------------------------------------------------------------------

    async def _find_reschedule_availability(
        self,
        booking_id: str,
        start: datetime,
    ) -> dict[str, Any] | None:
        target_start = start.astimezone(
            timezone.utc
        )

        search_end = target_start + timedelta(
            hours=24
        )

        data = await self._request(
            "POST",
            "/v2/bookings/availability/search",
            json={
                "query": {
                    "filter": {
                        "start_at_range": {
                            "start_at": (
                                self._square_datetime(
                                    target_start
                                )
                            ),
                            "end_at": (
                                self._square_datetime(
                                    search_end
                                )
                            ),
                        },
                        "booking_id": booking_id,
                    }
                }
            },
        )

        for availability in (
            data.get("availabilities")
            or []
        ):
            raw_start = availability.get(
                "start_at"
            )

            if not raw_start:
                continue

            if (
                self._parse_square_datetime(
                    raw_start
                )
                == target_start
            ):
                return availability

        return None

    async def move_booking(
        self,
        booking: ExternalBooking,
        start: datetime,
        end: datetime,
    ) -> ExternalBooking:
        if not booking.external_id:
            raise BookingProviderError(
                "Cannot reschedule a booking "
                "without a Square booking id"
            )

        booking_id = str(
            booking.external_id
        )

        truth(
            "RESCHEDULE_START",
            provider="square",
            target_external_booking_id=booking_id,
            requested_start=start.isoformat(),
        )

        current = await self._retrieve_booking(
            booking_id
        )

        current_location = str(current.get("location_id") or "")
        if current_location and current_location != self.location_id:
            raise BookingProviderError(
                "Existing Square booking belongs to a different location"
            )

        version = current.get("version")

        if version is None:
            raise BookingProviderError(
                "Square booking has no version"
            )

        # Confirm Square says the new slot is valid.
        availability = (
            await self._find_reschedule_availability(
                booking_id,
                start,
            )
        )

        if not availability:
            raise BookingProviderError(
                "The requested reschedule time "
                "is not available in Square"
            )

        new_start = availability.get(
            "start_at"
        )

        if not new_start:
            raise BookingProviderError(
                "Square reschedule availability "
                "has no start time"
            )

        idempotency_key = (
            self._idempotency_key(
                "reschedule",
                booking_id,
                version,
                new_start,
            )
        )

        data = await self._request(
            "PUT",
            f"/v2/bookings/{booking_id}",
            json={
                "idempotency_key": (
                    idempotency_key
                ),
                "booking": {
                    "version": version,
                    "start_at": new_start,
                },
            },
        )

        updated = (
            data.get("booking")
            or {}
        )

        if not updated.get("id"):
            raise BookingProviderError(
                "Square did not confirm the "
                "booking update"
            )

        truth(
            "RESCHEDULE_SUCCESS",
            provider="square",
            external_booking_id=updated.get("id") or booking_id,
        )

        return ExternalBooking(
            provider=self.provider,
            external_id=booking_id,
        )

    # ------------------------------------------------------------------
    # CANCEL
    # ------------------------------------------------------------------

    async def cancel_booking(
        self,
        booking: ExternalBooking,
    ) -> None:
        if not booking.external_id:
            raise BookingProviderError(
                "Cannot cancel a booking without "
                "a Square booking id"
            )

        booking_id = str(
            booking.external_id
        )

        current = await self._retrieve_booking(
            booking_id
        )

        version = current.get("version")

        if version is None:
            raise BookingProviderError(
                "Square booking has no version"
            )

        idempotency_key = (
            self._idempotency_key(
                "cancel",
                booking_id,
                version,
            )
        )

        data = await self._request(
            "POST",
            f"/v2/bookings/{booking_id}/cancel",
            json={
                "idempotency_key": (
                    idempotency_key
                ),
                "booking_version": version,
            },
        )

        cancelled = (
            data.get("booking")
            or {}
        )

        if not cancelled.get("id"):
            raise BookingProviderError(
                "Square did not confirm the "
                "booking cancellation"
            )