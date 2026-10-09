import re
from pydantic import BaseModel, Field, field_validator

OFFER_WORDS = set("would you like to consider service as an optional upgrade could we also look at for your visit are interested in another option may i offer explore a treatment choose instead this what about trying perhaps how does sound enhancement hear more details".split())

def validate_phrase(value):
    # Model-written invitations cannot carry factual claims or internal instructions.
    if len(value) > 200 or value.count("{service}") != 1 or not value.endswith("?"):
        raise ValueError("An invitation must contain {service} once and end in a question.")
    words = re.findall(r"[a-z]+", value.casefold())
    if len(words) > 22 or not words or words[0] not in {"would", "could", "are", "may", "what", "how"}:
        raise ValueError("Use a short optional invitation.")
    if set(words) - OFFER_WORDS or re.search(r"[^a-zA-Z\s{},?'.-]", value):
        raise ValueError("Invitation wording cannot include price, time, availability, health, popularity, or other claims.")
    if value.replace("{service}", "").count("{") or value.replace("{service}", "").count("}"):
        raise ValueError("Only the service placeholder is supported.")
    return value

class EnhancementRule(BaseModel):
    base_service: str = Field(min_length=1, max_length=255)
    target_service: str = Field(min_length=1, max_length=255)
    priority: int = Field(default=0, ge=0, le=100)
    # A provider-defined bundle or longer service, never separate partial writes.
    requires_resources: bool = False
    phrase_variants: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("phrase_variants")
    @classmethod
    def check_phrases(cls, values):
        return list(dict.fromkeys(validate_phrase(v.strip()) for v in values))

class EnhancementSettings(BaseModel):
    enabled: bool = False
    max_suggestions: int = Field(default=1, ge=0, le=1)
    personalize: bool = True
    retention_days: int = Field(default=90, ge=1, le=365)
    excluded_services: list[str] = Field(default_factory=list, max_length=100)
    rules: list[EnhancementRule] = Field(default_factory=list, max_length=100)
    # Only catalog-priced promotions are executable; no unsupported discount writes.
