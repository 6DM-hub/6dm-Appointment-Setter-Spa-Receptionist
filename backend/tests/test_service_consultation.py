import json

import pytest

from app.services.service_consultation import (
    ADD_ON_BENEFITS,
    classify_facial_need,
    classify_massage_need,
    configured_duration_choices,
    facial_consultation,
    massage_consultation,
    relevant_configured_addons,
)


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (("breakouts and congestion", "oily", "none"), "acne_clarifying"),
        (("dull", "dry by midday", "none"), "hydrating"),
        (("breakouts", "oily", "reactive and red"), "calming_barrier_repair"),
        (("uneven tone", "balanced", "none"), "custom"),
    ],
)
def test_facial_need_classification(answers, expected):
    assert classify_facial_need(*answers) == expected


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (("I have breakouts", "balanced", "none"), "acne_clarifying"),
        (("dryness and dullness", "balanced", "none"), "hydrating"),
        (("fine lines and wrinkles", "balanced", "none"), "custom"),
    ],
)
def test_facial_need_classification_accepts_natural_inflections(answers, expected):
    assert classify_facial_need(*answers) == expected


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (("I need to relax from stress", "all over", "light"), "relaxation"),
        (("specific tension", "shoulders", "medium"), "focused_therapeutic"),
        (("workout recovery", "legs", "deeper pressure"), "deeper_pressure_sports"),
        (("I'm unsure", "all over", "medium"), "customized"),
    ],
)
def test_massage_need_classification(answers, expected):
    assert classify_massage_need(*answers) == expected


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (("workout", "all over", "deeper"), "deeper_pressure_sports"),
        (("aches and soreness", "shoulders", "medium"), "focused_therapeutic"),
        (("tightness", "specific areas", "medium"), "focused_therapeutic"),
    ],
)
def test_massage_need_classification_accepts_natural_inflections(answers, expected):
    assert classify_massage_need(*answers) == expected


def test_facial_recommendation_returns_only_actual_configured_service():
    services = [
        {"name": "House Hydrating Facial", "duration_minutes": 30, "consultation_category": "hydrating", "id": "hydrating-30"},
        {"name": "House Hydrating Facial", "duration_minutes": 60, "consultation_category": "hydrating", "id": "hydrating-60"},
        {"name": "Custom Facial", "duration_minutes": 60, "id": "custom-60"},
    ]

    result = facial_consultation(
        services, main_concern="My skin looks dull", skin_feel="dry", sensitivity="none"
    )

    assert result["service"] == services[0]
    assert [choice["service"] for choice in result["durations"]["choices"]] == services[:2]
    assert result["durations"]["missing_minutes"] == []
    assert result["needs_clarification"] is False


def test_known_need_without_matching_catalog_service_is_not_invented():
    services = [{"name": "European Facial", "duration_minutes": 60}]

    result = facial_consultation(
        services, main_concern="acne and breakouts", skin_feel="oily"
    )

    assert result["category"] == "acne_clarifying"
    assert result["service"] is None
    assert result["durations"] == {"choices": [], "missing_minutes": [30, 60]}
    assert result["needs_clarification"] is True
    assert "Acne-Clarifying Facial" not in json.dumps(result)


def test_duration_choices_explicitly_report_missing_thirty_without_fabricating_it():
    sixty = {
        "name": "Therapeutic Massage",
        "duration_minutes": 60,
        "square_variation_id": "massage-60",
    }

    result = configured_duration_choices([sixty], sixty)

    assert result["choices"] == [{"minutes": 60, "service": sixty}]
    assert result["missing_minutes"] == [30]
    assert all(choice["service"].get("square_variation_id") for choice in result["choices"])


def test_duration_choices_match_named_variations_in_one_service_family():
    services = [
        {"name": "30 Minute Custom Facial", "duration_minutes": 30, "square_variation_id": "f30"},
        {"name": "60 Minute Custom Facial", "duration_minutes": 60, "square_variation_id": "f60"},
    ]

    result = configured_duration_choices(services, services[1])

    assert [choice["minutes"] for choice in result["choices"]] == [30, 60]
    assert [choice["service"]["square_variation_id"] for choice in result["choices"]] == ["f30", "f60"]
    assert result["missing_minutes"] == []


def test_duration_choices_exclude_addons_and_cross_modality_family_collisions():
    selected = {
        "name": "30 Minute Custom Facial",
        "duration_minutes": 30,
        "service_family": "Custom",
        "consultation_kind": "facial",
    }
    facial_sixty = {
        "name": "60 Minute Custom Facial",
        "duration_minutes": 60,
        "service_family": "Custom",
        "consultation_kind": "facial",
    }
    services = [
        selected,
        facial_sixty,
        {
            "name": "60 Minute Custom Massage",
            "duration_minutes": 60,
            "service_family": "Custom",
            "consultation_kind": "massage",
        },
        {
            "name": "Custom Add-on",
            "duration_minutes": 30,
            "service_family": "Custom",
            "is_add_on": True,
        },
    ]

    result = configured_duration_choices(services, selected)

    assert [choice["service"] for choice in result["choices"]] == [
        selected,
        facial_sixty,
    ]


def test_addons_require_relevance_owner_permission_and_catalog_presence():
    services = [
        {"name": "Therapeutic Massage", "duration_minutes": 60},
        {"name": "Hot Stones", "duration_minutes": 15},
        {"name": "CBD Oil", "duration_minutes": 5},
        {"name": "Aromatherapy", "duration_minutes": 5},
        {"name": "Extended Scalp", "duration_minutes": 15},
    ]
    rules = [{
        "base_service": "Therapeutic Massage",
        # Extended Scalp is configured but not owner-allowed. Foot Treatment is
        # owner-allowed but absent from the catalog. Aromatherapy is both, but
        # unrelated to the caller's stated tension/recovery need.
        "allowed_upsells": ["Hot Stones", "CBD Oil", "Aromatherapy", "Foot Treatment"],
    }]

    result = relevant_configured_addons(
        services,
        rules,
        base_service=services[0],
        stated_needs=("deep tension and recovery", "shoulders", "deep pressure"),
    )

    assert [item["service"]["name"] for item in result] == ["Hot Stones", "CBD Oil"]
    assert len(result) == 2
    assert result[0]["benefit"] == ADD_ON_BENEFITS["hot_stones"]
    assert result[1]["benefit"] == ADD_ON_BENEFITS["cbd_oil"]


def test_unconfigured_or_unapproved_addons_are_never_returned():
    services = [
        {"name": "Relaxation Massage", "duration_minutes": 60},
        {"name": "Aromatherapy", "duration_minutes": 5},
    ]
    no_owner_rule = relevant_configured_addons(
        services,
        [],
        base_service=services[0],
        stated_needs=("stress and relaxation",),
    )
    absent_catalog = relevant_configured_addons(
        services[:1],
        [{"base_service": "Relaxation Massage", "allowed_upsells": ["Aromatherapy"]}],
        base_service=services[0],
        stated_needs=("stress and relaxation",),
    )

    assert no_owner_rule == []
    assert absent_catalog == []


def test_massage_result_discards_injury_details_and_returns_only_safe_categories():
    services = [
        {"name": "Therapeutic Massage", "duration_minutes": 30},
        {"name": "Therapeutic Massage", "duration_minutes": 60},
        {"name": "Hot Stones", "duration_minutes": 15},
    ]
    rules = [{"base_service": "Therapeutic Massage", "allowed_upsells": ["Hot Stones"]}]
    sensitive_detail = "recent shoulder surgery - do not touch the incision"

    result = massage_consultation(
        services,
        rules,
        reason="specific muscle tension",
        areas="shoulders",
        pressure="medium",
        areas_to_avoid=sensitive_detail,
    )

    serialized = json.dumps(result)
    assert result["category"] == "focused_therapeutic"
    assert result["service"] == services[0]
    assert [choice["minutes"] for choice in result["durations"]["choices"]] == [30, 60]
    assert sensitive_detail not in serialized
    assert "surgery" not in serialized
    assert "incision" not in serialized


def test_unknown_massage_need_requests_clarification_without_defaulting():
    services = [{"name": "Swedish Massage", "duration_minutes": 60}]

    result = massage_consultation(
        services, [], reason="I want a massage", areas="all over", pressure="medium"
    )

    assert result["category"] is None
    assert result["service"] is None
    assert result["addons"] == []
    assert result["needs_clarification"] is True


def test_addons_follow_the_exact_duration_service_the_caller_selected():
    services = [
        {
            "name": "30 Minute Therapeutic Massage",
            "service_family": "Therapeutic Massage",
            "consultation_kind": "massage",
            "consultation_category": "focused_therapeutic",
            "duration_minutes": 30,
        },
        {
            "name": "60 Minute Therapeutic Massage",
            "service_family": "Therapeutic Massage",
            "consultation_kind": "massage",
            "consultation_category": "focused_therapeutic",
            "duration_minutes": 60,
        },
        {"name": "Hot Stones", "duration_minutes": 15, "is_add_on": True},
        {"name": "Extended Scalp", "duration_minutes": 15, "is_add_on": True},
    ]
    rules = [
        {
            "base_service": "30 Minute Therapeutic Massage",
            "allowed_upsells": ["Extended Scalp"],
        },
        {
            "base_service": "60 Minute Therapeutic Massage",
            "allowed_upsells": ["Hot Stones"],
        },
    ]

    before_choice = massage_consultation(
        services, rules, reason="tight muscles", areas="shoulders", pressure="medium"
    )
    chosen = massage_consultation(
        services,
        rules,
        reason="tight muscles",
        areas="shoulders",
        pressure="medium",
        selected_service_name="60 Minute Therapeutic Massage",
        selected_duration_minutes=60,
    )

    assert before_choice["addons"] == []
    assert chosen["selected_service"]["duration_minutes"] == 60
    assert [item["service"]["name"] for item in chosen["addons"]] == ["Hot Stones"]


def test_safe_category_reuses_addon_relevance_without_raw_answers():
    services = [
        {
            "name": "60 Minute Therapeutic Massage",
            "service_family": "Therapeutic Massage",
            "consultation_kind": "massage",
            "consultation_category": "focused_therapeutic",
            "duration_minutes": 60,
        },
        {"name": "Hot Stones", "duration_minutes": 15, "is_add_on": True},
        {"name": "Aromatherapy", "duration_minutes": 5, "is_add_on": True},
    ]
    rules = [{
        "base_service": "60 Minute Therapeutic Massage",
        "allowed_upsells": ["Hot Stones", "Aromatherapy"],
    }]

    result = massage_consultation(
        services,
        rules,
        reason=None,
        areas=None,
        pressure=None,
        category_hint="focused_therapeutic",
        selected_service_name="60 Minute Therapeutic Massage",
        selected_duration_minutes=60,
    )

    assert result["category"] == "focused_therapeutic"
    assert [item["service"]["name"] for item in result["addons"]] == ["Hot Stones"]
    assert "shoulder" not in json.dumps(result).casefold()


def test_consultation_category_fallback_cannot_cross_service_modality():
    services = [
        {
            "name": "Hydrating Massage",
            "consultation_kind": "massage",
            "consultation_category": "hydrating",
            "duration_minutes": 60,
        },
        {
            "name": "House Hydration",
            "consultation_kind": "facial",
            "consultation_category": "hydrating",
            "duration_minutes": 60,
        },
    ]

    result = facial_consultation(
        services, main_concern="dullness", skin_feel="dry", sensitivity="none"
    )

    assert result["service"]["name"] == "House Hydration"
