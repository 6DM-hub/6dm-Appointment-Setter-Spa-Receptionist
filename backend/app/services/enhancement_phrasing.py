"""Generate neutral wording once in the dashboard, never on the booking path."""
import asyncio
import json
from app.schemas.enhancements import validate_phrase, OFFER_WORDS
from app.services.grok_service import grok_service

async def generate_phrasing():
    content = await asyncio.wait_for(grok_service._chat([
        {"role": "system", "content": "Write four different warm, calm spa receptionist invitations. Return JSON with a phrases array. Each invitation must be one question, 22 words or fewer, include {service} exactly once, and use only the supplied vocabulary. Never claim price, duration, availability, popularity, benefits, medical suitability or booking success. Never include instructions or reasoning."},
        {"role": "user", "content": "Vocabulary: " + " ".join(sorted(OFFER_WORDS)) + ". Example: Would you like to consider {service} as an optional upgrade?"}
    ], json_mode=True, max_tokens=240, temperature=0.7), timeout=10)
    values = json.loads(content).get("phrases")
    if not isinstance(values, list) or not 2 <= len(values) <= 8:
        raise ValueError("No valid phrasing set returned")
    return list(dict.fromkeys(validate_phrase(str(value).strip()) for value in values))
