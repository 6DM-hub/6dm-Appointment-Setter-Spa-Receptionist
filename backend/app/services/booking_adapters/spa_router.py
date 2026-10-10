"""Spa Receptionist booking strategy.

`SpaBookingAdapter` is a router, not an integration: it owns the rules that
apply to *every* spa (business hours, service duration defaults) and delegates
the actual write to whichever provider the tenant configured — falling back to
the tenant's Google/internal calendar when that provider is unimplemented or
missing credentials.

That fallback is what makes onboarding config-only. A new `SpaAccount` row with
`booking_provider = "mindbody"` and an empty `booking_config` still answers
calls and books guests today; filling in the credentials later upgrades it in
place with no deploy.

Service resolution lives here, once, for every provider: the caller's phrase is
matched against the tenant's own service menu and the result is one of
resolved / ambiguous / unrecognized / unspecified. Ambiguous and unrecognized
results carry the real menu options so the agent can ask a specific question
("the 60 or the 90 minute European Facial?") instead of hitting a dead end.
"""
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.scheduling_time import utc_instant, elapsed_end
from app.core.config import settings
from app.models.spa_account import BookingProvider, SpaAccount
from app.services.booking_adapters.base import (
    AvailabilityVerdict,
    BookingAdapter,
    BookingContext,
    BookingProviderError,
    ExternalBooking,
    ProviderNotConfigured,
)
from app.services.booking_adapters.customers import (
    CustomerLookupOutcome,
    CustomerLookupResult,
)
from app.services.booking_adapters.saved_payments import (
    SavedPaymentLookup,
    SavedPaymentLookupOutcome,
)
from app.services.booking_adapters.google_calendar import GoogleCalendarAdapter
from app.services.booking_adapters.providers import VERTICAL_PROVIDERS
from app.services.booking_config import decrypt_config, missing_config
from app.services.business_hours import is_open_between
from app.services.truth_log import truth

logger = logging.getLogger(__name__)

# Most options read aloud in one clarification message. A long menu spoken to a
# caller is worse than a short "which kind of facial?" follow-up.
MAX_OPTIONS_IN_MESSAGE = 8

_DURATION_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*[-–—]?\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

_NUMBER_WORD_PATTERN = r"(?:(?:(?:one|two|three|four|five|six|seven|eight|nine))[\s-]+hundred(?:[\s-]+(?:and[\s-]+)?(?:(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?|(?:ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)|(?:one|two|three|four|five|six|seven|eight|nine)))?|(?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)(?:[\s-]+(?:one|two|three|four|five|six|seven|eight|nine))?|(?:ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen)|(?:one|two|three|four|five|six|seven|eight|nine)|hundred)"
_DURATION_NAME_RE = re.compile(
    rf"\b(?:\d+(?:\.\d+)?|{_NUMBER_WORD_PATTERN})"
    r"\s*[-–—]?\s*(hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)


def _minutes_in(text: str | None) -> int | None:
    """A duration the text mentions ("90 minutes", "an hour"), in minutes."""
    lowered = (text or "").casefold()
    if not lowered:
        return None
    if re.search(r"\bhour and a half\b", lowered):
        return 90
    if re.search(r"\bhalf( an)? hour\b", lowered):
        return 30
    match = _DURATION_RE.search(lowered)
    if match:
        value = float(match.group(1))
        return int(round(value * 60)) if match.group(2).startswith("h") else int(round(value))
    if re.search(r"\b(an|one) hour\b", lowered):
        return 60
    return None


def _entry_minutes(entry: dict[str, Any]) -> int | None:
    """Duration of a menu entry: its configured field, else parsed from its name."""
    duration = entry.get("duration_minutes")
    if isinstance(duration, int) and not isinstance(duration, bool) and duration > 0:
        return duration
    return _minutes_in(str(entry.get("name", "")))


def _option_label(entry: dict[str, Any]) -> str:
    """How an option is read to the caller: name, plus duration when useful."""
    name = str(entry.get("name", "")).strip()
    minutes = _entry_minutes(entry)
    if minutes and not re.search(rf"\b{minutes}\b", name):
        return f"{name} ({minutes} min)"
    return name


_STAFF_STOP = {"the", "and", "a", "an", "service", "services", "treatment", "treatments"}


def _stems(value: str | None) -> set[str]:
    tokens = re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).split()
    stems: set[str] = set()
    for token in tokens:
        if token.endswith("s") and len(token) > 3:
            token = token[:-1]
        if token and token not in _STAFF_STOP:
            stems.add(token)
    return stems


def _compact_phrase(value: str | None) -> str:
    """Letters and digits only, so spacing and punctuation cannot change identity."""
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _name_matches(requested: str, names: list[str]) -> bool:
    wanted = requested.casefold().strip()
    for name in names:
        current = name.casefold().strip()
        if wanted == current or wanted in current or current in wanted:
            return True
    return False


def _parse_slot_start(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        return utc_instant(parsed)
    except ValueError:
        return None


def _unique(labels: list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for label in labels:
        if label and label not in seen:
            seen.append(label)
    return tuple(seen)


@dataclass(frozen=True)
class ServiceResolution:
    """Outcome of matching the caller's words against the tenant's menu.

    status:
      resolved      exactly one menu entry (`entry`)
      ambiguous     more than one entry fits; `options` lists them
      unrecognized  nothing on the menu fits; `options` lists the menu
      unspecified   the caller has not named a service yet; `options` lists the menu
    """

    status: str
    entry: dict[str, Any] | None = None
    requested: str | None = None
    options: tuple[str, ...] = ()

    @property
    def name(self) -> str | None:
        if self.entry is None:
            return None
        return str(self.entry.get("name", "")).strip() or None


@dataclass(frozen=True)
class ProviderResolution:
    """Outcome of matching caller wording against the spa's configured team."""

    status: str
    member: dict[str, Any] | None = None
    requested: str | None = None
    options: tuple[str, ...] = ()

    @property
    def name(self) -> str | None:
        if self.member is None:
            return None
        return str(self.member.get("name", "")).strip() or None

    @property
    def provider_id(self) -> str | None:
        if self.member is None:
            return None
        value = str(
            self.member.get("square_team_member_id")
            or self.member.get("provider_id")
            or self.member.get("id")
            or ""
        ).strip()
        return value or None


class ProviderNotConfiguredAdapter(BookingAdapter):
    provider = "unconfigured"

    def __init__(self, spa_name: str, provider: str) -> None:
        self.provider = provider
        self.calendar_label = f"{spa_name}'s {provider} calendar"
        self.default_title = "Spa Service Appointment"

    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        raise ProviderNotConfigured(
            f"{self.provider} is not configured or has no live booking adapter"
        )


class SpaBookingAdapter(BookingAdapter):
    def __init__(self, spa: SpaAccount) -> None:
        self.spa = spa
        self.delegate = self._select_delegate(spa)
        self.provider = self.delegate.provider
        self.calendar_label = f"{spa.name}'s service calendar"
        self.default_title = "Spa Service Appointment"
        self.default_duration_minutes = self._default_duration(spa)

    @property
    def supports_customer_lookup(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.delegate, "supports_customer_lookup", False))

    @property
    def supports_customer_creation(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.delegate, "supports_customer_creation", False))

    @property
    def supports_saved_payment_method_lookup(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.delegate, "supports_saved_payment_method_lookup", False))

    async def find_customers_by_phone(self, phone: str) -> CustomerLookupResult:
        if not self.supports_customer_lookup:
            return CustomerLookupResult(outcome=CustomerLookupOutcome.UNSUPPORTED)
        return await self.delegate.find_customers_by_phone(phone)

    async def get_customer(self, external_customer_id: str) -> CustomerLookupResult:
        if not self.supports_customer_lookup:
            return CustomerLookupResult(outcome=CustomerLookupOutcome.UNSUPPORTED)
        return await self.delegate.get_customer(external_customer_id)

    async def create_customer(self, ctx: BookingContext) -> CustomerLookupResult:
        if not self.supports_customer_creation:
            return CustomerLookupResult(outcome=CustomerLookupOutcome.UNSUPPORTED)
        return await self.delegate.create_customer(ctx)

    async def update_customer(
        self, external_customer_id: str, ctx: BookingContext
    ) -> CustomerLookupResult:
        """Delegates an explicit correction. Booking resolution does not call this."""
        if not self.supports_customer_lookup:
            return CustomerLookupResult(outcome=CustomerLookupOutcome.UNSUPPORTED)
        return await self.delegate.update_customer(external_customer_id, ctx)

    async def list_saved_payment_methods(self, external_customer_id: str) -> SavedPaymentLookup:
        if not self.supports_saved_payment_method_lookup:
            return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.UNSUPPORTED)
        return await self.delegate.list_saved_payment_methods(external_customer_id)

    async def has_usable_saved_payment_method(
        self, external_customer_id: str
    ) -> SavedPaymentLookup:
        if not self.supports_saved_payment_method_lookup:
            return SavedPaymentLookup(outcome=SavedPaymentLookupOutcome.UNSUPPORTED)
        return await self.delegate.has_usable_saved_payment_method(external_customer_id)

    @property
    def supports_save_card_on_file(self) -> bool:  # type: ignore[override]
        return bool(getattr(self.delegate, "supports_save_card_on_file", False))

    async def save_card_on_file(
        self,
        *,
        external_customer_id: str,
        source_id: str,
        idempotency_key: str,
        verification_token: str | None = None,
    ):
        """Save a card on file. Delegates only when this provider implements it."""
        from app.services.booking_adapters.saved_payments import SaveCardOutcome, SaveCardResult

        if not self.supports_save_card_on_file:
            return SaveCardResult(outcome=SaveCardOutcome.UNSUPPORTED)
        return await self.delegate.save_card_on_file(
            external_customer_id=external_customer_id,
            source_id=source_id,
            idempotency_key=idempotency_key,
            verification_token=verification_token,
        )

    # -- wiring ----------------------------------------------------------- #
    @staticmethod
    def _select_delegate(spa: SpaAccount) -> BookingAdapter:
        config = decrypt_config(spa.booking_config)
        fallback = GoogleCalendarAdapter(
            calendar_id=config.get("google_calendar_id") or getattr(getattr(spa, "google_calendar_connection", None), "selected_calendar_id", None),
            calendar_label=f"{spa.name}'s service calendar",
            default_title="Spa Service Appointment",
            connection=getattr(spa, "google_calendar_connection", None),
            timezone_name=spa.timezone,
        )

        if spa.booking_provider == BookingProvider.GOOGLE_CALENDAR:
            if missing_config(spa.booking_provider, config):
                return ProviderNotConfiguredAdapter(spa.name, spa.booking_provider.value)
            return fallback

        provider_cls = VERTICAL_PROVIDERS.get(spa.booking_provider)
        if provider_cls is None:
            logger.warning(
                "Spa %s requests unknown provider %r; using the calendar fallback.",
                spa.name,
                spa.booking_provider,
            )
            return fallback

        missing = provider_cls.missing_config_keys(config)
        if missing:
            return ProviderNotConfiguredAdapter(spa.name, spa.booking_provider.value)

        if not provider_cls.implemented:
            return ProviderNotConfiguredAdapter(spa.name, spa.booking_provider.value)

        delegate = provider_cls(spa.name, config)
        if getattr(spa, "timezone", None):
            delegate.timezone_name = spa.timezone
        return delegate

    @staticmethod
    def _default_duration(spa: SpaAccount) -> int:
        for service in spa.services or []:
            duration = service.get("duration_minutes")
            if isinstance(duration, int) and duration > 0:
                return duration
        return settings.DEFAULT_APPOINTMENT_DURATION_MINUTES

    @staticmethod
    def _service_tokens(value: str | None) -> tuple[str, ...]:
        # Remove explicit duration phrases before tokenizing, but preserve bare
        # numbers/number-words because they may be part of a service/package
        # name (e.g. "HydroLux 5" or speech-to-text "HydroLux five").
        text = _DURATION_NAME_RE.sub(" ", (value or ""))
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

    # -- service resolution ------------------------------------------------ #
    def _menu(self) -> list[dict[str, Any]]:
        return [
            service
            for service in (self.spa.services or [])
            if isinstance(service, dict) and str(service.get("name", "")).strip()
        ]

    def _menu_options(self, menu: list[dict[str, Any]]) -> tuple[str, ...]:
        return _unique([_option_label(entry) for entry in menu])

    def resolve_service(self, service_description: str | None) -> ServiceResolution:
        """Match the caller's phrase against the tenant-approved service menu.

        Explicit duration phrases are stripped from the *name* tokens (so
        "60 minute massage" and "sixty minute massage" still find "Massage"),
        while bare numbers/number-words that may belong to a service name are
        preserved. Duration is still used as its own tie-breaker when present.
        """
        menu = self._menu()
        menu_options = self._menu_options(menu)
        requested = (service_description or "").strip()

        # The default title is what the context carries when the caller has not
        # named a service at all; it is not a service the caller asked for.
        if not requested or requested.casefold() == self.default_title.casefold():
            return ServiceResolution("unspecified", options=menu_options)

        requested_tokens = self._service_tokens(requested)
        requested_compact = _compact_phrase(requested)
        if not requested_tokens and len(requested_compact) < 6:
            return ServiceResolution("unspecified", requested=requested, options=menu_options)
        requested_set = set(requested_tokens)

        candidates: list[tuple[dict[str, Any], tuple[str, ...]]] = []
        seen_entries: set[int] = set()
        for entry in menu:
            labels = [str(entry.get("name", "")).strip()]
            aliases = entry.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            labels.extend(str(alias).strip() for alias in aliases if str(alias).strip())
            name_tokens = self._service_tokens(labels[0])
            matched = False
            for label in labels:
                candidate_tokens = self._service_tokens(label)
                candidate_set = set(candidate_tokens)
                candidate_compact = _compact_phrase(label)
                token_match = bool(candidate_tokens) and (
                    requested_tokens == candidate_tokens
                    or requested_set.issubset(candidate_set)
                    or candidate_set.issubset(requested_set)
                    or (
                        bool(requested_tokens)
                        and requested_tokens == candidate_tokens[: len(requested_tokens)]
                    )
                    or (
                        bool(candidate_tokens)
                        and candidate_tokens == requested_tokens[: len(candidate_tokens)]
                    )
                )
                compact_match = (
                    len(requested_compact) >= 6
                    and len(candidate_compact) >= 6
                    and (
                        requested_compact == candidate_compact
                        or candidate_compact.startswith(requested_compact)
                        or requested_compact.startswith(candidate_compact)
                    )
                )
                if token_match or compact_match:
                    matched = True
                    break
            if matched and id(entry) not in seen_entries:
                seen_entries.add(id(entry))
                candidates.append((entry, name_tokens or requested_tokens))

        if not candidates:
            truth(
                "SERVICE_RESOLUTION_NEEDS_CLARIFICATION",
                status="unrecognized",
            )
            return ServiceResolution("unrecognized", requested=requested, options=menu_options)

        wanted_minutes = _minutes_in(requested)
        if wanted_minutes:
            timed = [entry for entry, _tokens in candidates if _entry_minutes(entry) == wanted_minutes]
            if len(timed) == 1:
                truth(
                    "SERVICE_RESOLUTION_SUCCESS",
                    service_id=str(timed[0].get("id") or timed[0].get("name") or ""),
                    variation_id=str(timed[0].get("square_variation_id") or "none"),
                )
                return ServiceResolution("resolved", entry=timed[0], requested=requested)

        if len(candidates) == 1:
            truth(
                "SERVICE_RESOLUTION_SUCCESS",
                service_id=str(candidates[0][0].get("id") or candidates[0][0].get("name") or ""),
                variation_id=str(candidates[0][0].get("square_variation_id") or "none"),
            )
            return ServiceResolution("resolved", entry=candidates[0][0], requested=requested)

        # More than one loose match: an exact name match wins over a partial one,
        # so "European Facial" is not made ambiguous by "European Facial Deluxe".
        exact = [entry for entry, tokens in candidates if tokens == requested_tokens]
        pool = exact if exact else [entry for entry, _tokens in candidates]
        if len(pool) == 1:
            truth(
                "SERVICE_RESOLUTION_SUCCESS",
                service_id=str(pool[0].get("id") or pool[0].get("name") or ""),
                variation_id=str(pool[0].get("square_variation_id") or "none"),
            )
            return ServiceResolution("resolved", entry=pool[0], requested=requested)

        if wanted_minutes:
            timed_pool = [entry for entry in pool if _entry_minutes(entry) == wanted_minutes]
            if len(timed_pool) == 1:
                truth(
                    "SERVICE_RESOLUTION_SUCCESS",
                    service_id=str(timed_pool[0].get("id") or timed_pool[0].get("name") or ""),
                    variation_id=str(timed_pool[0].get("square_variation_id") or "none"),
                )
                return ServiceResolution("resolved", entry=timed_pool[0], requested=requested)
            if timed_pool:
                pool = timed_pool

        truth("SERVICE_RESOLUTION_NEEDS_CLARIFICATION", status="ambiguous")
        return ServiceResolution(
            "ambiguous",
            requested=requested,
            options=_unique([_option_label(entry) for entry in pool]),
        )

    def resolve_provider(self, staff_name: str | None) -> "ProviderResolution":
        """Match caller wording to the spa's configured team. Never invent a person."""
        requested = " ".join((staff_name or "").split())
        roster = [
            member
            for member in (self.spa.staff or [])
            if isinstance(member, dict) and str(member.get("name") or "").strip()
        ]
        if not requested:
            return ProviderResolution("unspecified")
        if not roster:
            return ProviderResolution("unconfigured", requested=requested)
        matches: list[dict[str, Any]] = []
        for member in roster:
            labels = [str(member.get("name") or "").strip()]
            aliases = member.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            labels.extend(str(alias).strip() for alias in aliases if str(alias).strip())
            if _name_matches(requested, labels):
                matches.append(member)
        if len(matches) == 1:
            member = matches[0]
            provider_id = str(
                member.get("square_team_member_id")
                or member.get("provider_id")
                or member.get("id")
                or ""
            ).strip() or None
            truth(
                "PROVIDER_RESOLUTION_SUCCESS",
                provider_id=provider_id or "name_only",
            )
            return ProviderResolution("resolved", member=member, requested=requested)
        if len(matches) > 1:
            return ProviderResolution(
                "ambiguous",
                requested=requested,
                options=tuple(str(member.get("name") or "").strip() for member in matches),
            )
        return ProviderResolution(
            "unrecognized",
            requested=requested,
            options=tuple(str(member.get("name") or "").strip() for member in roster),
        )

    def resolve_service_name(self, service_description: str | None) -> str | None:
        """Use the tenant-approved service menu as the source of truth."""
        resolution = self.resolve_service(service_description)
        return resolution.name if resolution.status == "resolved" else None

    def requested_services(self, description):
        from app.services.visit_booking import parts
        return parts(self, description)

    @staticmethod
    def requested_duration_minutes(service_description: str | None) -> int | None:
        """Return only a duration the caller explicitly attached to one service."""
        return _minutes_in(service_description)

    def requires_visit_validation(self, ctx):
        if (ctx.selected_slot or {}).get("visit_segments") or len(self.requested_services(ctx.service_description or ctx.title)) > 1:
            return True
        entry = self.resolve_service(ctx.service_description or ctx.title).entry or {}
        rules = (getattr(self.spa, "booking_policies", None) or {}).get("visit", {})
        return bool(any(entry.get(k) for k in ("resource_ids", "preparation_buffer_minutes", "transition_buffer_minutes", "cleanup_buffer_minutes"))
                    or rules.get("special_hours") or rules.get("allow_after_hours")
                    or any(m.get("hours") or m.get("special_hours") for m in self.spa.staff or []))

    def duration_for_service(self, service_description: str | None) -> int:
        """Match against the tenant's configured services so voice variants map to the same duration."""
        if not service_description:
            return self.default_duration_minutes

        requested = self.requested_services(service_description)
        if len(requested) > 1:
            return sum(self.duration_for_service(item) for item in requested)
        resolution = self.resolve_service(service_description)
        if resolution.status == "resolved" and resolution.entry is not None:
            duration = resolution.entry.get("duration_minutes")
            if isinstance(duration, int) and duration > 0:
                return duration

        wanted = service_description.casefold()
        for service in self.spa.services or []:
            name = str(service.get("name", "")).casefold()
            duration = service.get("duration_minutes")
            if name and isinstance(duration, int) and duration > 0:
                if name in wanted or wanted in name:
                    return duration
        return self.default_duration_minutes

    @staticmethod
    def _clarification_reason(resolution: ServiceResolution) -> str:
        """The verdict reason for a service that could not be resolved.

        Format: `needs_clarification: <code>[: <what was asked>][ | options: a; b]`.
        The options are the tenant's real menu entries, so the agent can ask a
        specific question. The booking service parses this same format.
        """
        code = {
            "ambiguous": "service_ambiguous",
            "unrecognized": "service_not_recognized",
        }.get(resolution.status, "service_not_specified")
        reason = f"needs_clarification: {code}"
        if resolution.requested and resolution.status in {"ambiguous", "unrecognized"}:
            reason += f": {resolution.requested}"
        if resolution.options:
            shown = resolution.options[:MAX_OPTIONS_IN_MESSAGE]
            reason += " | options: " + "; ".join(shown)
        return reason

    def _apply_entry(self, ctx: BookingContext, entry: dict[str, Any]) -> BookingContext:
        """Rewrite the context to the resolved menu entry.

        Also attaches an explicitly configured `square_variation_id` /
        `square_variation_version` from that entry, so Square's own resolution
        never has to guess from catalog text for a service the tenant has
        already mapped.
        """
        canonical_name = str(entry.get("name", "")).strip()
        overrides: dict[str, Any] = {
            "title": canonical_name,
            "service_description": canonical_name,
        }
        variation_id = entry.get("square_variation_id")
        if variation_id:
            overrides["service_variation_id"] = str(variation_id)
            version = entry.get("square_variation_version")
            if version is not None:
                overrides["service_variation_version"] = version
        return replace(ctx, **overrides)

    def _canonicalize_ctx(self, ctx: BookingContext) -> tuple[BookingContext, str | None]:
        """Resolve the caller's phrase against the tenant's own menu ONCE.

        `check_availability` used to rewrite `ctx.title`/`service_description`
        to the canonical name but `create_booking`/`move_booking` did not —
        they forwarded the caller's raw words straight to the provider a
        second time, which could resolve to a different service (or fail to
        resolve at all) than the one availability had just approved. Routing
        every provider call through this one function keeps them consistent.
        """
        requested_service = ctx.service_description or ctx.title
        resolution = self.resolve_service(requested_service)
        if resolution.status != "resolved" or resolution.entry is None:
            return ctx, None
        return self._apply_entry(ctx, resolution.entry), resolution.name

    async def booking_timezone_name(self) -> str | None:
        """Expose the provider zone for validation against the business zone."""
        return await self.delegate.booking_timezone_name()

    async def describe_location(self) -> dict[str, Any] | None:
        describe = getattr(self.delegate, "describe_location", None)
        if callable(describe):
            return await describe()
        return None

    def eligible_staff_names(self, service_name: str | None) -> list[str] | None:
        """People assigned to this service on the dashboard.

        None means the spa has not assigned services to anyone, so booking
        stays unrestricted. A list (possibly empty) means assignments exist
        and only those people may be offered.
        """
        staff = [member for member in (self.spa.staff or []) if isinstance(member, dict)]
        assigned_any = False
        matched: list[str] = []
        requested = _stems(service_name or "")
        for member in staff:
            name = str(member.get("name") or "").strip()
            listed = member.get("services") or []
            if not name or not listed:
                continue
            assigned_any = True
            if requested and any(_stems(str(item)) & requested for item in listed):
                matched.append(name)
        if not matched:
            # This service is not limited to a named provider. Other services
            # that do have assignments stay limited to those people.
            return None
        return matched

    def _staff_context(self, ctx: BookingContext) -> tuple[BookingContext, str | None]:
        requested = (ctx.preferred_staff or "").strip()
        resolved = self.resolve_provider(requested or None)
        if resolved.status == "unrecognized":
            shown = ", ".join(resolved.options[:MAX_OPTIONS_IN_MESSAGE])
            return ctx, (
                f"{requested} is not on this spa's team. "
                f"The configured providers are: {shown}. "
                "Ask which provider they want, or offer any available provider."
            )
        if resolved.status == "ambiguous":
            return ctx, (
                f"More than one provider matches {requested}: "
                f"{', '.join(resolved.options)}. Ask which provider they mean."
            )
        if resolved.status == "resolved" and resolved.name:
            ctx = replace(
                ctx,
                preferred_staff=resolved.name,
                provider_id=resolved.provider_id or ctx.provider_id,
            )
            requested = resolved.name
        allowed = self.eligible_staff_names(ctx.service_description or ctx.title)
        if allowed is None:
            return ctx, None
        if not allowed:
            return ctx, (
                "No team member on the dashboard is assigned to that service. "
                "Do not book it with anyone else."
            )
        if requested and not _name_matches(requested, allowed):
            return ctx, (
                f"{requested} is not assigned to that service. "
                f"Only {', '.join(allowed)} can be booked for it."
            )
        if requested:
            return ctx, None
        return replace(ctx, allowed_staff=tuple(allowed)), None

    def _slot_inside_hours(self, slot: dict[str, Any], fallback_minutes: int) -> bool:
        start = _parse_slot_start(slot.get("start"))
        if start is None:
            return False
        try:
            minutes = int(slot.get("duration_minutes") or fallback_minutes or 60)
        except (TypeError, ValueError):
            minutes = fallback_minutes or 60
        end = elapsed_end(start, max(minutes, 1))
        return is_open_between(self.spa.business_hours, self.spa.timezone, start, end)

    def _inside_hours(self, slots: list[dict[str, Any]] | tuple, fallback_minutes: int) -> list[dict[str, Any]]:
        return [slot for slot in slots if self._slot_inside_hours(slot, fallback_minutes)]

    def _apply_dashboard_hours(
        self, verdict: AvailabilityVerdict, ctx: BookingContext
    ) -> AvailabilityVerdict:
        """Square can be open earlier than the dashboard. Both have to agree."""
        provider_zone = getattr(self.delegate, "timezone_name", None)
        if provider_zone and provider_zone != self.spa.timezone:
            raise BookingProviderError("Square and the business timezone differ. Verify settings before booking.", code="LOCATION_TIMEZONE")
        minutes = max(int((ctx.end - ctx.start).total_seconds() / 60), 1)
        if verdict.available and verdict.slot and self._slot_inside_hours(verdict.slot, minutes):
            truth("BUSINESS_HOURS_VALIDATED", open=True, reason="within_hours")
            return AvailabilityVerdict(available=True, slot=verdict.slot)
        later = list(getattr(self.delegate, "_last_range_slots", None) or [])
        seen: set[str] = set()
        combined: list[dict[str, Any]] = []
        for slot in [*verdict.alternatives, *later]:
            key = str(slot.get("start") or "")
            if key in seen:
                continue
            seen.add(key)
            combined.append(slot)
        alternatives = tuple(self._inside_hours(combined, minutes))
        truth(
            "BUSINESS_HOURS_VALIDATED",
            open=False if verdict.available else True,
            reason="outside_business_hours" if verdict.available else "alternatives_filtered",
        )
        if verdict.available:
            return AvailabilityVerdict.no(
                "outside_business_hours: that time is outside the spa's opening hours "
                "on the dashboard. Offer only a time inside those hours.",
                alternatives=alternatives,
            )
        return AvailabilityVerdict.no(verdict.reason or "unavailable", alternatives=alternatives)

    async def list_openings(self, ctx: BookingContext, range_start, range_end):
        if self.requires_visit_validation(ctx):
            from app.services.visit_booking import list_visits
            return await list_visits(self, ctx, range_start, range_end)
        ctx, staff_block = self._staff_context(ctx)
        if staff_block:
            return []
        if self.delegate.provider == "square":
            requested_service = ctx.service_description or ctx.title
            resolution = self.resolve_service(requested_service)
            square_ctx = (
                self._apply_entry(ctx, resolution.entry)
                if resolution.status == "resolved" and resolution.entry is not None
                else ctx
            )
            slots = await self.delegate.list_openings(square_ctx, range_start, range_end)
            provider_zone = getattr(self.delegate, "timezone_name", None)
            if provider_zone and provider_zone != self.spa.timezone:
                raise BookingProviderError("Provider and business timezone differ.", code="LOCATION_TIMEZONE")
            minutes = max(int((ctx.end - ctx.start).total_seconds() / 60), 1)
            return self._inside_hours(slots, minutes)
        slots = await self.delegate.list_openings(ctx, range_start, range_end)
        minutes = max(int((utc_instant(ctx.end) - utc_instant(ctx.start)).total_seconds() / 60), 1)
        return self._inside_hours(slots, minutes)

    # -- BookingAdapter ---------------------------------------------------- #
    async def check_availability(self, ctx: BookingContext) -> AvailabilityVerdict:
        if self.requires_visit_validation(ctx):
            from app.services.visit_booking import check_visit
            return await check_visit(self, ctx)
        # Square is the operational source of truth.  Do not reject a Square
        # request using potentially stale local business hours or a stale local
        # service menu before Square is even queried.
        #
        # If the local menu has an exact/canonical mapping, use it as an alias
        # (especially its square_variation_id).  Otherwise pass the caller's
        # phrase through so Square's Catalog + SearchAvailability decide whether
        # the service/location/time is actually bookable.
        if self.delegate.provider == "square":
            requested_service = ctx.service_description or ctx.title
            resolution = self.resolve_service(requested_service)
            # Dashboard menu is used only to disambiguate configured variants
            # (HydroLux5 Face vs Face+Neck). It must not invent availability.
            # Unrecognized phrases still go to Square Catalog so a real catalog
            # service is not blocked by a stale local menu.
            if self.spa.services and resolution.status in {"ambiguous", "unspecified"}:
                return AvailabilityVerdict.no(self._clarification_reason(resolution))
            square_ctx = (
                self._apply_entry(ctx, resolution.entry)
                if resolution.status == "resolved" and resolution.entry is not None
                else ctx
            )
            square_ctx, staff_block = self._staff_context(square_ctx)
            if staff_block:
                return AvailabilityVerdict.no(staff_block)
            verdict = await self.delegate.check_availability(square_ctx)
            return self._apply_dashboard_hours(verdict, square_ctx)

        # Non-Square providers still use the tenant configuration as the
        # operational guard unless/until those integrations expose equivalent
        # live business/service metadata.
        if not is_open_between(
            self.spa.business_hours, self.spa.timezone, ctx.start, ctx.end
        ):
            return AvailabilityVerdict.no(
                "outside_business_hours: "
                f"{self.spa.name} is closed at that time. Offer a slot inside "
                "the spa's opening hours instead."
            )

        if not self.spa.services:
            return await self.delegate.check_availability(ctx)

        requested_service = ctx.service_description or ctx.title
        resolution = self.resolve_service(requested_service)
        if resolution.status != "resolved" or resolution.entry is None:
            return AvailabilityVerdict.no(self._clarification_reason(resolution))

        return await self.delegate.check_availability(
            self._apply_entry(ctx, resolution.entry)
        )

    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        if self.requires_visit_validation(ctx):
            from app.services.visit_booking import create_visit
            return await create_visit(self, ctx)
        canonical_ctx, canonical_name = self._canonicalize_ctx(ctx)
        try:
            return await self.delegate.create_booking(
                canonical_ctx if canonical_name else ctx
            )
        except BookingProviderError:
            logger.exception(
                "Provider %s failed to create a booking for spa %s",
                self.delegate.provider,
                self.spa.name,
            )
            raise

    async def move_booking(
        self, booking: ExternalBooking, start, end
    ) -> ExternalBooking:
        try:
            return await self.delegate.move_booking(booking, start, end)
        except BookingProviderError:
            logger.exception(
                "Provider %s failed to move a booking for spa %s",
                self.delegate.provider,
                self.spa.name,
            )
            raise

    async def cancel_booking(self, booking: ExternalBooking) -> None:
        try:
            await self.delegate.cancel_booking(booking)
        except BookingProviderError:
            logger.exception(
                "Provider %s failed to cancel a booking for spa %s",
                self.delegate.provider,
                self.spa.name,
            )
