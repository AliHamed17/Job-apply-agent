from __future__ import annotations

from profile.cv_routing import (
    CVDefinition,
    CVRoutingConfig,
    RoutingJob,
    RoutingOverride,
    route_cv,
)
from profile.models import UserProfile

from submitters.form_brain import FieldSpec, FormBrain


def _config(minimum: float = 0.3) -> CVRoutingConfig:
    return CVRoutingConfig(
        minimum_confidence=minimum,
        cvs=[
            CVDefinition(
                id="software",
                file="software.pdf",
                title_terms=["software", "backend"],
                skills=["python", "api", "sql"],
                seniority=["junior", "senior"],
            ),
            CVDefinition(
                id="ai",
                file="ai.pdf",
                title_terms=["machine", "learning", "data"],
                skills=["python", "pytorch", "sql"],
                seniority=["junior", "senior"],
            ),
        ],
    )


def test_routing_is_deterministic_and_auditable() -> None:
    job = RoutingJob(
        title="Machine Learning Engineer",
        description="Build PyTorch systems with Python",
        seniority="senior",
        required_skills=["pytorch"],
    )
    first = route_cv(job, _config())
    second = route_cv(job, _config())
    assert first == second
    assert first.selected_cv_id == "ai"
    assert first.confidence >= 0.3
    assert any(item.startswith("title:") for item in first.matched_evidence)


def test_ordered_override_wins() -> None:
    config = _config()
    config.overrides = [
        RoutingOverride(cv_id="software", title_contains=["machine learning"])
    ]
    decision = route_cv(RoutingJob(title="Machine Learning Engineer"), config)
    assert decision.selected_cv_id == "software"
    assert decision.overridden
    assert decision.matched_evidence == ["ordered_override"]


def test_routing_abstains_below_threshold() -> None:
    decision = route_cv(
        RoutingJob(title="Chief Happiness Officer", description="culture"),
        _config(minimum=0.8),
    )
    assert decision.selected_cv_id is None
    assert decision.fallback_reason == "abstained_low_confidence"


async def test_sensitive_answer_requires_confirmed_evidence() -> None:
    profile = UserProfile()
    profile.personal.work_authorization = "Yes"
    brain = FormBrain(profile)
    result = await brain.answer(
        FieldSpec(label="Are you authorized to work in this country?", kind="radio", required=True),
        job=None,
    )
    assert result.value is None
    assert result.source == "confirmed_evidence_required"
    assert not result.confident


async def test_sensitive_answer_uses_only_user_confirmed_fact() -> None:
    profile = UserProfile()
    profile.evidence.user_confirmed["work authorization"] = "Yes"
    brain = FormBrain(profile)
    result = await brain.answer(
        FieldSpec(label="Work authorization", kind="radio", required=True),
        job=None,
    )
    assert result.value == "Yes"
    assert result.source == "user_confirmed"
    assert result.confident
