from app.services.booking_adapters.providers.base import VerticalProviderAdapter


class ZenotiAdapter(VerticalProviderAdapter):
    """Zenoti API v1 (`/v1/bookings`)."""

    provider = "zenoti"
    required_config_keys = ("api_key", "center_id")
    api_docs = "https://docs.zenoti.com"
    implemented = False
