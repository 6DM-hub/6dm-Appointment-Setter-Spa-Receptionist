from app.services.grok_service import build_realtime_instructions
from app.services.receptionist_identity import incoming_greeting


def test_business_name_and_private_pronunciation():
    spoken = incoming_greeting("YourDaySpa")
    assert spoken == (
        "Thank you for calling YourDaySpa. I’m Cara, your AI receptionist. "
        "May I help you reserve an appointment today?"
    )
    assert "CARE-uh" not in spoken
    instructions = build_realtime_instructions("YourDaySpa", "Your name is Old Name.")
    assert instructions.index("Your name is always Cara") > instructions.index("Old Name")
    assert '"My name is Cara."' in instructions
    assert "CARE-uh" in instructions
    assert "wait for the caller's response" in instructions
    assert "rescheduling, cancellation" in instructions
