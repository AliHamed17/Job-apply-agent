"""Private feature prompts must never cross a cloud LLM transport."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from profile.models import Personal, Resume, UserProfile
from typing import Any

import pytest

from jobs.models import JobData
from llm.client import LLMClient
from llm.contracts import (
    DataClassification,
    GenerationPurpose,
    ModelIdentity,
)
from llm.culture_fit import evaluate_culture_fit
from llm.followup_planner import generate_followup_plan
from llm.interview_prep import generate_interview_prep
from llm.interview_simulator import evaluate_interview_answer
from llm.outreach import generate_outreach
from llm.salary_negotiator import generate_salary_brief

FeatureCall = Callable[[JobData, UserProfile, LLMClient], Awaitable[object]]


def _outreach(job: JobData, profile: UserProfile, client: LLMClient) -> Awaitable[object]:
    return generate_outreach(job, profile, client=client)


def _culture_fit(job: JobData, profile: UserProfile, client: LLMClient) -> Awaitable[object]:
    return evaluate_culture_fit(job, profile, client=client)


def _interview_prep(job: JobData, profile: UserProfile, client: LLMClient) -> Awaitable[object]:
    return generate_interview_prep(job, profile, client=client)


def _followup(job: JobData, profile: UserProfile, client: LLMClient) -> Awaitable[object]:
    return generate_followup_plan(job, profile, client=client)


def _salary(job: JobData, profile: UserProfile, client: LLMClient) -> Awaitable[object]:
    return generate_salary_brief(job, profile, client=client)


def _simulation(job: JobData, profile: UserProfile, client: LLMClient) -> Awaitable[object]:
    return evaluate_interview_answer(
        question="How did you solve the problem?",
        candidate_answer="I investigated the issue and documented the result.",
        job=job,
        profile=profile,
        client=client,
    )


_FEATURES: tuple[tuple[str, FeatureCall, GenerationPurpose], ...] = (
    ("outreach", _outreach, GenerationPurpose.OUTREACH),
    ("culture_fit", _culture_fit, GenerationPurpose.CULTURE_FIT),
    ("interview_prep", _interview_prep, GenerationPurpose.INTERVIEW_PREP),
    ("followup", _followup, GenerationPurpose.FOLLOWUP),
    ("salary", _salary, GenerationPurpose.SALARY),
    ("interview_simulation", _simulation, GenerationPurpose.INTERVIEW_SIMULATION),
)


def _job_and_profile() -> tuple[JobData, UserProfile]:
    return (
        JobData(
            title="Platform Engineer",
            company="Example",
            description="Build reliable services.",
        ),
        UserProfile(
            personal=Personal(name="Candidate", location="Local"),
            resume=Resume(
                text=(
                    "Built reliable services.\n"
                    "Citizenship is private and must not enter the model prompt."
                )
            ),
        ),
    )


class _CloudTrapClient(LLMClient):
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.transport_calls = 0

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(provider=self.provider, model="cloud-test", local=False)

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        del prompt, system, max_tokens, temperature
        self.transport_calls += 1
        raise AssertionError("cloud transport must not be called")

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        del prompt, system, max_tokens
        self.transport_calls += 1
        raise AssertionError("cloud transport must not be called")


@pytest.mark.asyncio
@pytest.mark.parametrize("app_env", ("development", "test", "production"))
@pytest.mark.parametrize("provider", ("openai", "anthropic"))
@pytest.mark.parametrize(
    ("feature_name", "call", "_purpose"),
    _FEATURES,
    ids=[entry[0] for entry in _FEATURES],
)
async def test_private_feature_never_reaches_cloud_transport_in_any_environment(
    monkeypatch,
    app_env: str,
    provider: str,
    feature_name: str,
    call: FeatureCall,
    _purpose: GenerationPurpose,
) -> None:
    del feature_name, _purpose
    monkeypatch.setenv("APP_ENV", app_env)
    job, profile = _job_and_profile()
    client = _CloudTrapClient(provider)

    result = await call(job, profile, client)

    assert result is not None
    assert client.transport_calls == 0
    serialized = repr(result).casefold()
    assert "jenkins" not in serialized
    assert "groovy" not in serialized
    assert "75%" not in serialized
    assert "3 production llm" not in serialized


class _LocalSchemaClient(LLMClient):
    def __init__(self) -> None:
        self.typed_calls: list[dict[str, Any]] = []

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(provider="mock", model="local-schema-test", local=True)

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        del prompt, system, max_tokens, temperature
        raise AssertionError("untyped generation must not be used")

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict:
        del prompt, system, max_tokens
        raise AssertionError("untyped generation must not be used")

    async def generate_typed(self, **kwargs: Any):
        self.typed_calls.append(kwargs)
        return await super().generate_typed(**kwargs)

    async def _generate_schema_payload(
        self,
        *,
        prompt: str,
        system: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        deadline,
        attempt: int,
    ) -> dict[str, Any]:
        del prompt, system, max_tokens, temperature, deadline, attempt
        properties = schema["properties"]
        if "linkedin_note" in properties:
            return {
                "linkedin_note": "Hello",
                "email_subject": "Application",
                "email_body": "Hello hiring team.",
            }
        if "culture_fit_score" in properties:
            return {
                "culture_fit_score": 80,
                "cultural_highlights": [],
                "behavioral_talking_points": [],
                "caution_flags": [],
            }
        if "predicted_questions" in properties:
            return {
                "predicted_questions": ["How do you design reliable services?"],
                "star_story_talking_points": [],
                "interviewer_questions": ["What is the first priority?"],
            }
        if "stage1_day3_checkin" in properties:
            return {
                "stage1_day3_checkin": "Following up.",
                "stage2_day7_value_add": "I remain interested.",
                "stage3_day14_inquiry": "May I ask for an update?",
            }
        if "estimated_percentiles" in properties:
            return {
                "currency": "ILS",
                "estimated_percentiles": {
                    "p25": 1,
                    "p50": 2,
                    "p75": 3,
                    "p90": 4,
                },
                "negotiation_talking_points": [],
                "counter_offer_script": "Thank you for the offer.",
            }
        return {
            "score": 80,
            "strengths": [],
            "missing_points": [],
            "improved_answer": "A clearer answer.",
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feature_name", "call", "purpose"),
    _FEATURES,
    ids=[entry[0] for entry in _FEATURES],
)
async def test_private_feature_uses_typed_private_local_boundary(
    feature_name: str,
    call: FeatureCall,
    purpose: GenerationPurpose,
) -> None:
    del feature_name
    job, profile = _job_and_profile()
    client = _LocalSchemaClient()

    await call(job, profile, client)

    assert len(client.typed_calls) == 1
    request = client.typed_calls[0]
    assert request["purpose"] is purpose
    assert request["data_classification"] is DataClassification.PRIVATE_APPLICATION
    assert "Citizenship is private" not in request["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("feature_name", "call", "_purpose"),
    _FEATURES,
    ids=[entry[0] for entry in _FEATURES],
)
async def test_private_feature_abstains_when_no_safe_cv_context_remains(
    feature_name: str,
    call: FeatureCall,
    _purpose: GenerationPurpose,
) -> None:
    del feature_name, _purpose
    job, profile = _job_and_profile()
    profile.resume.text = "Nationality: private."
    client = _LocalSchemaClient()

    result = await call(job, profile, client)

    assert result is not None
    assert client.typed_calls == []
