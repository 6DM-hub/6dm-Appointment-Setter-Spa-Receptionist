"""Registry of vertical spa-booking platforms.

Adding a new platform is a two-step change: implement a
`VerticalProviderAdapter` subclass here, then add its value to
`app.models.spa_account.BookingProvider`. Nothing in the call path or the
routers needs to know about it.
"""
from app.models.spa_account import BookingProvider
from app.services.booking_adapters.providers.base import VerticalProviderAdapter
from app.services.booking_adapters.providers.mangomint import MangomintAdapter
from app.services.booking_adapters.providers.mindbody import MindbodyAdapter
from app.services.booking_adapters.providers.square import SquareAdapter
from app.services.booking_adapters.providers.vagaro import VagaroAdapter
from app.services.booking_adapters.providers.zenoti import ZenotiAdapter

#: google_calendar is deliberately absent — it is handled by
#: GoogleCalendarAdapter, which is also the fallback for everything here.
VERTICAL_PROVIDERS: dict[BookingProvider, type[VerticalProviderAdapter]] = {
    BookingProvider.MINDBODY: MindbodyAdapter,
    BookingProvider.MANGOMINT: MangomintAdapter,
    BookingProvider.SQUARE: SquareAdapter,
    BookingProvider.VAGARO: VagaroAdapter,
    BookingProvider.ZENOTI: ZenotiAdapter,
}

__all__ = [
    "VERTICAL_PROVIDERS",
    "VerticalProviderAdapter",
    "MindbodyAdapter",
    "MangomintAdapter",
    "SquareAdapter",
    "VagaroAdapter",
    "ZenotiAdapter",
]
