from app.services.booking_adapters.providers.base import VerticalProviderAdapter


class MindbodyAdapter(VerticalProviderAdapter):
    """Mindbody Public API v6 (`/appointment/addappointment`)."""

    provider = "mindbody"
    required_config_keys = ("site_id", "api_key", "source_name", "source_password")
    api_docs = "https://developers.mindbodyonline.com/PublicDocumentation/V6"
    implemented = False
