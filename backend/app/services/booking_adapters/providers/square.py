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
        from app.services.scheduling_time import business_zone
        try:
            self.timezone_name = business_zone(location.get("timezone")).key
        except ValueError as exc:
            self._location_cache = None
            raise BookingProviderError("Square location has no valid IANA timezone", code="LOCATION_TIMEZONE") from exc
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

        # Square uses "Massage -" as a category prefix while spa menus may
        # say simply "Deep Tissue". Ignore that category for specific services,
        # keeping modifiers such as "Couples" part of the service identity.
        if requested_set - {"massage"}:
            requested_set.discard("massage")
            item_set.discard("massage")
            variation_set.discard("massage")
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
        from app.services.scheduling_time import utc_instant
        return utc_instant(datetime.fromisoformat(value.replace("Z", "+00:00")))

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

        Retrying the SAME operation gener`self._location()

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
                ctx.booking_reference or ctx.customer_phone,
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

        from app.services.square_booking_recovery import create_with_recovery
        data = await create_with_recovery(self, payload)

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
        if square_status != "ACCEPTED":
            truth(
                "BOOKING_CREATE_REJECTED",
                provider="square",
                external_booking_id=booking_id,
                square_status=square_status,
                reason="inactive_booking",
            )
            raise BookingProviderError(
                "Square did not create an active booking.",
                code="BOOKING_OUTCOME_UNKNOWN" if square_status in {"", "PENDING"} else "INACTIVE_BOOKING",
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
