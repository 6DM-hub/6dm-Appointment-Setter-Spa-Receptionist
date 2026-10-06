"""Shared scaffolding for vertical spa-booking platforms.

Each provider declares the `SpaAccount.booking_config` keys it needs and
whether a live client exists yet. `SpaBookingAdapter` consults both before
delegating, so an unimplemented or half-configured provider degrades to the
tenant's Google/internal calendar instead of raising mid-call.
"""
import logging
from typing import Any

from app.services.booking_adapters.base import (
    BookingAdapter,
    BookingContext,
    ExternalBooking,
    ProviderNotConfigured,
)

logger = logging.getLogger(__name__)


class VerticalProviderAdapter(BookingAdapter):
    #: Keys required in SpaAccount.booking_config for this provider to work.
    required_config_keys: tuple[str, ...] = ()
    #: False until a real API client is written for this provider.
    implemented: bool = False
    #: Stubs have no customer directory until that adapter is actually built.
    supports_customer_lookup = False
    supports_customer_creation = False
    supports_saved_payment_method_lookup = False
    supports_save_card_on_file = False
    #: Docs for whoever picks the integration up next.
    api_docs: str = ""

    def __init__(self, spa_name: str, config: dict[str, Any] | None = None) -> None:
        self.spa_name = spa_name
        self.config = config or {}
        self.calendar_label = f"{spa_name}'s {self.provider} calendar"

    @classmethod
    def missing_config_keys(cls, config: dict[str, Any] | None) -> list[str]:
        config = config or {}
        return [key for key in cls.required_config_keys if not config.get(key)]

    @classmethod
    def is_usable(cls, config: dict[str, Any] | None) -> bool:
        return cls.implemented and not cls.missing_config_keys(config)

    async def create_booking(self, ctx: BookingContext) -> ExternalBooking:
        raise ProviderNotConfigured(
            f"{self.provider} write-back is not implemented yet"
            + (f" (see {self.api_docs})" if self.api_docs else "")
        )
