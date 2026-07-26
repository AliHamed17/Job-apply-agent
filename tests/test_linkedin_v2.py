from __future__ import annotations

from profile.models import UserProfile
from typing import Any

import pytest

from llm.client import LLMClient
from llm.contracts import (
    DataClassification,
    GenerationPurpose,
    ModelIdentity,
    TypedGeneration,
)
from submitters.form_brain import FieldSpec, FormBrain
from submitters.linkedin_v2 import resolve_step


class _TypedLLM(LLMClient):
    def __init__(self, mapping: dict[str, tuple[str, str]]) -> None:
        self.mapping = mapping
        self.calls = 0

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(provider="test", model="typed-local", local=True)

    async def generate(self, *args: Any, **kwargs: Any) -> str:
        del args, kwargs
        raise AssertionError("plain generation is prohibited")

    async def generate_json(self, *args: Any, **kwargs: Any) -> dict:
        del args, kwargs
        raise AssertionError("untyped JSON generation is prohibited")

    async def generate_typed(self, **kwargs: Any) -> TypedGeneration:
        self.calls += 1
        prompt = str(kwargs["prompt"]).casefold()
        answer = next(
            (pair for key, pair in self.mapping.items() if key.casefold() in prompt),
            (None, None),
        )
        response_model = kwargs["response_model"]
        value = response_model.model_validate({"value": answer[0], "evidence_quote": answer[1]})
        return TypedGeneration(
            value=value,
            model_identity=self.model_identity,
            purpose=GenerationPurpose(kwargs["purpose"]),
            prompt_version=kwargs["prompt_version"],
            data_classification=DataClassification(kwargs["data_classification"]),
            attempts=1,
        )


def _profile() -> UserProfile:
    profile = UserProfile()
    profile.personal.name = "Example Candidate"
    profile.personal.email = "candidate@example.test"
    profile.resume.text = "10 years RF."
    return profile


@pytest.mark.asyncio
async def test_resolve_step_fills_answerable_fields() -> None:
    fields = [
        FieldSpec("Email", "text", [], True),
        FieldSpec("Years of RF experience", "number", [], True),
    ]
    client = _TypedLLM({"years of rf": ("10", "10 years RF.")})
    brain = FormBrain(_profile(), client=client, db=None)

    plan = await resolve_step(fields, brain, job=None)

    assert plan.fills["Email"] == "candidate@example.test"
    assert plan.fills["Years of RF experience"] == "10"
    assert plan.blocked_by is None
    assert client.calls == 1


@pytest.mark.asyncio
async def test_resolve_step_blocks_sensitive_required_without_llm() -> None:
    fields = [
        FieldSpec(
            "Do you hold a Secret clearance?",
            "radio",
            ["Yes", "No"],
            True,
        )
    ]
    client = _TypedLLM({})
    brain = FormBrain(_profile(), client=client, db=None)

    plan = await resolve_step(fields, brain, job=None)

    assert plan.blocked_by == "Do you hold a Secret clearance?"
    assert client.calls == 0
