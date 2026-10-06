from app.services.booking_adapters.providers.base import VerticalProviderAdapter


class MangomintAdapter(VerticalProviderAdapter):
    """Mangomint GraphQL API."""

    provider = "mangomint"
    required_config_keys = ("api_key", "location_id")
    api_docs = "https://developers.mangomint.com"
    implemented = False
