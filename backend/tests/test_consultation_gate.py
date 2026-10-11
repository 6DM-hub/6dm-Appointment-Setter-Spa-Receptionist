import json

import pytest

from app.services.consultation_gate import (
    availability_gate,
    consultation_complete,
    facial_consultation_staff_note,
    infer_consultation_kind,
    initialize_consultation,
    record_consultation_answer,
    record_pending_answer,
    record_consultation_permission,
    record_duration_selection,
    take_duration_offer,
    take_massage_addon_offer,
    take_next_question,
)


FACIAL_SERVICES = [
    {
        "name": "Petite Facial",
        "duration_minutes": 30,
        "consultation_kind": "facial",
        "consultation_category": "custom",
    },
    {
        "name": "European Facial",
        "duration_minutes": 60,
        "consultation_kind": "facial",
        "consultation_category": "custom",
    },
]

MASSAGE_SERVICES = [
    {
        "name": "30 Minute Swedish Massage",
        "duration_minutes": 30,
        "service_family": "Swedish Massage",
        "consultation_kind": "massage",
    },
    {
        "name": "60 Minute Swedish Massage",
        "duration_minutes": 60,
        "service_family": "Swedish Massage",
        "consultation_kind": "massage",
    },
    {
        "name": "Hot Stones",
        "duration_minutes": 15,
        "is_add_on": True,
    },
    {
        "name": "CBD Oil",
        "duration_minutes": 5,
        "is_add_on": True,
    },
    {
        "name": "Aromatherapy",
        "duration_minutes": 5,
        "is_add_on": True,
    },
]


def _answer_all(state, answers):
    for field, answer in answers:
        state = record_consultation_answer(state, field=field, answer=answer)
    return state


@pytest.mark.parametrize(
    ("service", "expected"),
    [
        ("60-minute European Facial", "facial"),
        ("Swedish massage", "massage"),
        ({"name": "House Ritual", "consultation_kind": "facial"}, "facial"),
        ("brow wax", None),
    ],
)
def test_infers_only_supported_consultation_modalities(service, expected):
    assert infer_consultation_kind(service) == expected


def test_questions_are_ordered_and_never_repeated_while_awaiting_answer():
    state = initialize_consultation("30 minute facial")

    first = take_next_question(state)
    repeated = take_next_question(first["state"])
    answered = record_consultation_answer(
        repeated["state"], field="main_concern", answer="dry and dull"
    )
    second = take_next_question(answered)

    assert first["status"] == "ask_question"
    assert first["question"]["field"] == "main_concern"
    assert repeated["status"] == "awaiting_answer"
    assert repeated["question"] is None
    assert second["question"]["field"] == "skin_feel"


def test_latest_caller_utterance_credits_other_clearly_volunteered_details():
    state = initialize_consultation("facial")
    asked = take_next_question(state)
    recorded = record_pending_answer(
        asked["state"], caller_utterance="Mostly dryness and dullness"
    )

    assert recorded["status"] == "answer_recorded"
    assert recorded["answered_field"] == "main_concern"
    assert recorded["state"]["answered_fields"] == ["main_concern", "skin_feel"]
    assert recorded["state"]["category"] == "hydrating"
    assert "mostly dryness and dullness" not in json.dumps(recorded["state"]).casefold()


def test_caller_turn_cannot_skip_a_question_that_server_has_not_asked():
    state = initialize_consultation("facial")
    result = record_pending_answer(state, caller_utterance="dry")

    assert result["status"] == "no_pending_question"
    assert result["state"]["answered_fields"] == []


def test_facial_consultation_requires_every_question():
    state = initialize_consultation("European Facial")
    state = _answer_all(
        state,
        [
            ("main_concern", "breakouts"),
            ("skin_feel", "oily"),
        ],
    )
    assert consultation_complete(state) is False
    decision = take_next_question(state)
    assert decision["question"]["field"] == "active_breakouts"

    done = record_consultation_answer(
        decision["state"], field="active_breakouts", answer="no active breakouts"
    )
    done = record_consultation_answer(
        done, field="skin_sensitivity", answer="no sensitivity or redness"
    )
    done = record_consultation_answer(
        done, field="facial_history", answer="no prior facials"
    )
    assert consultation_complete(done) is True
    assert take_next_question(done)["status"] == "complete"


def test_massage_safety_answer_is_never_persisted():
    injury_text = "I had shoulder surgery and the incision is still tender"
    state = initialize_consultation("60 minute Swedish Massage")
    state = _answer_all(
        state,
        [
            ("massage_reason", "relaxation"),
            ("massage_areas", "shoulders"),
            ("pressure_preference", "medium"),
            ("safety_answered", injury_text),
        ],
    )

    serialized = json.dumps(state).casefold()
    assert consultation_complete(state) is True
    assert "safety_answered" in state["answered_fields"]
    assert "surgery" not in serialized
    assert "incision" not in serialized
    assert "shoulder" not in serialized


def test_state_keeps_only_safe_category_not_raw_consultation_answers():
    state = initialize_consultation("massage")
    state = record_consultation_answer(
        state, field="massage_reason", answer="I have deep muscle tension after recovery"
    )

    serialized = json.dumps(state).casefold()
    assert state["category"] == "deeper_pressure_sports"
    assert "muscle tension" not in serialized
    assert "recovery" not in serialized


def test_facial_staff_note_contains_only_structured_consented_details():
    state = initialize_consultation("facial")
    assert state is not None
    state = record_consultation_permission(state, accepted=True)
    state = record_consultation_answer(
        state,
        field="main_concern",
        answer="My exact private wording says dry, dull skin and clogged pores",
    )
    state = record_consultation_answer(
        state,
        field="skin_feel",
        answer="Dry and tight by midday",
    )
    state = record_consultation_answer(
        state,
        field="active_breakouts",
        answer="I don't have active breakouts",
    )
    state = record_consultation_answer(
        state,
        field="skin_sensitivity",
        answer="No redness, but I am sensitive to some products",
    )
    state = record_consultation_answer(
        state,
        field="facial_history",
        answer="I've had facials and liked hydration but not harsh exfoliation",
    )

    note = facial_consultation_staff_note(state)

    assert note is not None
    assert "dryness" in note
    assert "dullness" in note
    assert "clogged pores" in note
    assert "active breakouts: not reported" in note
    assert "sensitivity/redness/product irritation: reported" in note
    assert "liked: gentle hydration" in note
    assert "prefers to avoid: exfoliation" in note
    assert "exact private wording" not in note


def test_declined_facial_questions_do_not_create_a_staff_note():
    state = initialize_consultation("facial")
    assert state is not None
    state = record_consultation_permission(state, accepted=False)

    assert facial_consultation_staff_note(state) is None


def test_negated_sensitivity_does_not_override_a_hydrating_need():
    state = initialize_consultation("facial")
    assert state is not None
    state = record_consultation_answer(
        state,
        field="main_concern",
        answer="My skin is dry and dull, with no sensitivity or redness",
    )

    assert state["category"] == "hydrating"


def test_positive_sensitivity_after_negated_redness_uses_calming_category():
    state = initialize_consultation("facial")
    assert state is not None
    state = record_consultation_answer(
        state,
        field="skin_sensitivity",
        answer="No redness, but I am sensitive to some products",
    )

    assert state["category"] == "calming_barrier_repair"


@pytest.mark.parametrize("answer", ["No", "This is my first facial", "No, this is my first one"])
def test_first_time_facial_answers_are_structured_as_no_prior_facial(answer):
    state = initialize_consultation("facial")
    assert state is not None
    state = record_consultation_permission(state, accepted=True)
    state = record_consultation_answer(
        state, field="facial_history", answer=answer
    )

    assert state["facial_summary"]["prior_facials"] == "no"


def test_explicit_duration_survives_later_model_call_that_omits_it():
    state = initialize_consultation(
        "60 minute Swedish Massage", requested_duration_minutes=60
    )
    resumed = initialize_consultation(
        "Swedish Massage", prior_state=state
    )

    assert resumed["requested_duration_minutes"] == 60
    assert resumed["selected_duration_minutes"] == 60


def test_explicit_caller_duration_change_replaces_prior_selection():
    state = initialize_consultation("60 minute Swedish Massage")
    changed = initialize_consultation(
        "30 minute Swedish Massage", prior_state=state
    )

    assert changed["requested_duration_minutes"] == 30
    assert changed["selected_duration_minutes"] == 30


def test_duration_offer_waits_for_completed_consultation():
    state = initialize_consultation("Petite Facial")
    result = take_duration_offer(state, FACIAL_SERVICES)

    assert result["status"] == "consultation_incomplete"
    assert result["offer"] is None


def test_duration_offer_fills_missing_family_duration_from_same_modality():
    state = initialize_consultation("Petite Facial", services=FACIAL_SERVICES)
    state = _answer_all(
        state,
        [
            ("main_concern", "dullness"),
            ("skin_feel", "dry"),
                ("active_breakouts", "none"),
                ("skin_sensitivity", "none"),
                ("facial_history", "no prior facials"),
        ],
    )
    result = take_duration_offer(
        state, FACIAL_SERVICES, recommended_service=FACIAL_SERVICES[0]
    )

    assert result["status"] == "offer_duration"
    assert result["offer"]["choices"] == [
        {"minutes": 30, "service_name": "Petite Facial"},
        {"minutes": 60, "service_name": "European Facial"},
    ]
    assert "focused start" in result["offer"]["text"]
    assert "more time to target your concern" in result["offer"]["text"]


def test_duration_offer_never_invents_missing_catalog_duration():
    services = [FACIAL_SERVICES[1]]
    state = initialize_consultation("European Facial", services=services)
    state = _answer_all(
        state,
        [
            ("main_concern", "uneven tone"),
            ("skin_feel", "balanced"),
                ("active_breakouts", "none"),
                ("skin_sensitivity", "none"),
                ("facial_history", "no prior facials"),
        ],
    )
    result = take_duration_offer(state, services)

    assert result["offer"]["choices"] == [
        {"minutes": 60, "service_name": "European Facial"}
    ]
    assert "30-minute" not in result["offer"]["text"]


def test_same_family_duration_options_win_over_other_same_modality_services():
    services = MASSAGE_SERVICES[:2] + [
        {
            "name": "30 Minute Sports Massage",
            "duration_minutes": 30,
            "consultation_kind": "massage",
        }
    ]
    state = initialize_consultation("60 Minute Swedish Massage", services=services)
    state = _answer_all(
        state,
        [
            ("massage_reason", "relaxation"),
            ("massage_areas", "all over"),
            ("pressure_preference", "light"),
            ("safety_answered", "none"),
        ],
    )
    result = take_duration_offer(state, services)

    assert result["offer"]["choices"][0]["service_name"] == "30 Minute Swedish Massage"
    assert result["offer"]["choices"][1]["service_name"] == "60 Minute Swedish Massage"


def test_duration_offer_is_returned_once_and_preserves_explicit_request():
    state = initialize_consultation(
        "60 Minute Swedish Massage", services=MASSAGE_SERVICES
    )
    state = _answer_all(
        state,
        [
            ("massage_reason", "relaxation"),
            ("massage_areas", "all over"),
            ("pressure_preference", "light"),
            ("safety_answered", "none"),
        ],
    )
    first = take_duration_offer(state, MASSAGE_SERVICES)
    second = take_duration_offer(first["state"], MASSAGE_SERVICES)

    assert first["state"]["requested_duration_minutes"] == 60
    assert first["state"]["selected_duration_minutes"] == 60
    assert first["state"]["selected_service_name"] == "60 Minute Swedish Massage"
    assert second["status"] == "already_presented"
    assert second["offer"] is None


def test_explicit_named_service_wins_when_classification_recommends_another():
    services = MASSAGE_SERVICES[:2] + [
        {
            "name": "30 Minute Deep Tissue Massage",
            "duration_minutes": 30,
            "service_family": "Deep Tissue Massage",
            "consultation_kind": "massage",
        },
        {
            "name": "60 Minute Deep Tissue Massage",
            "duration_minutes": 60,
            "service_family": "Deep Tissue Massage",
            "consultation_kind": "massage",
        },
    ]
    state = initialize_consultation("60 Minute Swedish Massage", services=services)
    state = _answer_all(
        state,
        [
            ("massage_reason", "workout recovery"),
            ("massage_areas", "legs"),
            ("pressure_preference", "deep"),
            ("safety_answered", "none"),
        ],
    )
    result = take_duration_offer(
        state, services, recommended_service="60 Minute Deep Tissue Massage"
    )

    assert state["category"] == "deeper_pressure_sports"
    assert result["state"]["selected_service_name"] == "60 Minute Swedish Massage"
    assert result["state"]["selected_duration_minutes"] == 60
    assert [choice["service_name"] for choice in result["offer"]["choices"]] == [
        "30 Minute Swedish Massage",
        "60 Minute Swedish Massage",
    ]


def test_duration_selection_must_come_from_grounded_offer():
    state = initialize_consultation("massage")
    state = _answer_all(
        state,
        [
            ("massage_reason", "relaxation"),
            ("massage_areas", "all over"),
            ("pressure_preference", "light"),
            ("safety_answered", "none"),
        ],
    )
    offered = take_duration_offer(state, MASSAGE_SERVICES)

    with pytest.raises(ValueError, match="grounded catalog"):
        record_duration_selection(
            offered["state"], selected_duration_minutes=90
        )


def test_massage_addons_are_grounded_relevant_limited_and_offered_once():
    rules = [
        {
            "base_service": "60 Minute Swedish Massage",
            "allowed_upsells": ["Hot Stones", "CBD Oil", "Aromatherapy"],
        }
    ]
    state = initialize_consultation(
        "60 Minute Swedish Massage", services=MASSAGE_SERVICES
    )
    state = _answer_all(
        state,
        [
            ("massage_reason", "deep tension and workout recovery"),
            ("massage_areas", "upper back"),
            ("pressure_preference", "deep"),
            ("safety_answered", "none"),
        ],
    )
    duration = take_duration_offer(state, MASSAGE_SERVICES)
    first = take_massage_addon_offer(
        duration["state"], MASSAGE_SERVICES, rules
    )
    repeated = take_massage_addon_offer(first["state"], MASSAGE_SERVICES, rules)

    assert first["status"] == "offer_addons"
    assert [item["service_name"] for item in first["offer"]["items"]] == [
        "Hot Stones",
        "CBD Oil",
    ]
    assert all(item["description"] for item in first["offer"]["items"])
    assert len(first["offer"]["items"]) == 2
    assert repeated["status"] == "already_presented"
    assert repeated["offer"] is None


def test_unconfigured_or_unapproved_addons_are_not_offered():
    services = MASSAGE_SERVICES[:2] + [MASSAGE_SERVICES[-1]]
    rules = [
        {
            "base_service": "60 Minute Swedish Massage",
            "allowed_upsells": ["Hot Stones", "CBD Oil"],
        }
    ]
    state = initialize_consultation("60 Minute Swedish Massage", services=services)
    state = _answer_all(
        state,
        [
            ("massage_reason", "deep tension"),
            ("massage_areas", "back"),
            ("pressure_preference", "deep"),
            ("safety_answered", "none"),
        ],
    )
    state = take_duration_offer(state, services)["state"]
    result = take_massage_addon_offer(state, services, rules)

    assert result["status"] == "no_relevant_addons"
    assert result["offer"] is None


def test_addons_require_a_grounded_selected_duration():
    state = initialize_consultation("massage")
    state = _answer_all(
        state,
        [
            ("massage_reason", "relaxation"),
            ("massage_areas", "all over"),
            ("pressure_preference", "light"),
            ("safety_answered", "none"),
        ],
    )

    result = take_massage_addon_offer(state, MASSAGE_SERVICES, [])

    assert result["status"] == "duration_not_selected"
    assert result["offer"] is None


def test_availability_gate_requires_each_stage_in_order():
    rules = [
        {
            "base_service": "60 Minute Swedish Massage",
            "allowed_upsells": ["Hot Stones"],
        }
    ]
    state = initialize_consultation(
        "60 Minute Swedish Massage", services=MASSAGE_SERVICES
    )
    assert availability_gate(state) == {
        "allow": False,
        "status": "consultation_required",
    }

    state = _answer_all(
        state,
        [
            ("massage_reason", "deep tension"),
            ("massage_areas", "back"),
            ("pressure_preference", "deep"),
            ("safety_answered", "none"),
        ],
    )
    assert availability_gate(state)["status"] == "duration_offer_required"

    state = take_duration_offer(state, MASSAGE_SERVICES)["state"]
    assert availability_gate(state)["status"] == "addon_offer_required"

    state = take_massage_addon_offer(state, MASSAGE_SERVICES, rules)["state"]
    assert availability_gate(state) == {"allow": True, "status": "ready"}


def test_facial_availability_requires_duration_advice_once_but_no_addon_stage():
    state = initialize_consultation(
        "30 minute Petite Facial", services=FACIAL_SERVICES
    )
    state = _answer_all(
        state,
        [
            ("main_concern", "dull"),
            ("skin_feel", "dry"),
                ("active_breakouts", "none"),
                ("skin_sensitivity", "none"),
                ("facial_history", "no prior facials"),
        ],
    )
    assert availability_gate(state)["status"] == "duration_offer_required"

    state = take_duration_offer(state, FACIAL_SERVICES)["state"]
    assert availability_gate(state) == {"allow": True, "status": "ready"}
