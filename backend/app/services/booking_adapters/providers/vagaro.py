from app.services.booking_adapters.providers.base import VerticalProviderAdapter


class VagaroAdapter(VerticalProviderAdapter):
    """Vagaro Merchant API."""

    provider = "vagaro"
    required_config_keys = ("api_key", "business_id")
    api_docs = "https://developers.vagaro.com"
    implemented = False
