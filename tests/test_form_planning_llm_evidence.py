"""Unsupported typed LLM answers must never make a form plan eligible."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from profile.models import UserProfile
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.form_planning import (
    AnswerPolicyContext,
    AnswerPolicyV1,
    LLMFieldAnswerV1,
)
from core.submission_domain import FormFieldV1, FormPlanV1, ReasonCode
from llm.qualification_registry import load_qualified_local_model

_QUALIFIED_MODEL_DIGEST = load_qualified_local_model().digest


@pytest.fixture(autouse=True)
def _current_qualification_report(monkeypatch):
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: True,
    )


@pytest.mark.asyncio
async def test_local_form_prompt_removes_sensitive_values_hidden_under_benign_keys():
    class CapturingClient:
        model_identity = SimpleNamespace(local=True)

        def __init__(self):
            self.prompt = ""

        async def generate_typed(self, **kwargs):
            self.prompt = kwargs["prompt"]
            return SimpleNamespace(
                value=LLMFieldAnswerV1(
                    value="Python",
                    confidence=1.0,
                    evidence_refs=(f"cv:{'c' * 64}:primary_language",),
                ),
                model_identity=SimpleNamespace(
                    provider="ollama",
                    model="qwen2.5:7b",
                    local=True,
                    digest=_QUALIFIED_MODEL_DIGEST,
                ),
            )

    client = CapturingClient()
    profile = UserProfile.model_validate(
        {
            "evidence": {
                "user_confirmed": {"misc_note": "Canadian citizen"},
                "cv_extracted": {
                    "unscoped_fact_must_not_be_used": "Synthetic legacy value",
                },
                "cv_extracted_by_artifact": {
                    "c" * 64: {
                        "primary_language": "Python",
                        "misc_detail": "אזרחות קנדית",
                    }
                },
            }
        }
    )
    context = AnswerPolicyContext(
        profile=profile,
        profile_version=1,
        selected_cv_id="cv",
        selected_cv_hash="c" * 64,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
    )
    field = FormFieldV1(
        field_id="primary_language",
        canonical_name=None,
        label="Relevant technical experience",
        field_type="text",
        required=True,
        position=0,
    )

    result = await AnswerPolicyV1(llm_client=client).plan_fields((field,), context)

    assert result.decisions[0].value == "Python"
    assert "Canadian citizen" not in client.prompt
    assert "אזרחות קנדית" not in client.prompt


@pytest.mark.asyncio
async def test_local_form_prose_is_rendered_from_exact_cited_evidence():
    class ParaphrasingClient:
        model_identity = SimpleNamespace(local=True)

        async def generate_typed(self, **_kwargs):
            return SimpleNamespace(
                value=LLMFieldAnswerV1(
                    value="I have used Python extensively.",
                    confidence=1.0,
                    evidence_refs=(f"cv:{'c' * 64}:primary_language",),
                ),
                model_identity=SimpleNamespace(
                    provider="ollama",
                    model="qwen2.5:7b",
                    local=True,
                    digest=_QUALIFIED_MODEL_DIGEST,
                ),
            )

    exact_evidence = "Developed production services in Python"
    profile = UserProfile.model_validate(
        {"evidence": {"cv_extracted_by_artifact": {"c" * 64: {"primary_language": exact_evidence}}}}
    )
    context = AnswerPolicyContext(
        profile=profile,
        profile_version=1,
        selected_cv_id="cv",
        selected_cv_hash="c" * 64,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
    )
    field = FormFieldV1(
        field_id="primary_language",
        canonical_name=None,
        label="Relevant technical experience: Primary programming language",
        field_type="text",
        required=True,
        position=0,
    )

    result = await AnswerPolicyV1(llm_client=ParaphrasingClient()).plan_fields(
        (field,),
        context,
    )

    assert result.decisions[0].value == exact_evidence


@pytest.mark.asyncio
async def test_form_plan_never_combines_answers_from_different_model_digests():
    class SwappingModelClient:
        model_identity = SimpleNamespace(local=True)

        def __init__(self):
            self.calls = 0

        async def generate_typed(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                value = "Developed production services in Python"
                reference = f"cv:{'c' * 64}:primary_language"
                digest = _QUALIFIED_MODEL_DIGEST
            else:
                value = "Operated workloads on Kubernetes"
                reference = f"cv:{'c' * 64}:container_platform"
                digest = f"sha256:{'b' * 64}"
            return SimpleNamespace(
                value=LLMFieldAnswerV1(
                    value=value,
                    confidence=1.0,
                    evidence_refs=(reference,),
                ),
                model_identity=SimpleNamespace(
                    provider="ollama",
                    model="qwen2.5:7b",
                    local=True,
                    digest=digest,
                ),
            )

    profile = UserProfile.model_validate(
        {
            "evidence": {
                "cv_extracted_by_artifact": {
                    "c" * 64: {
                        "primary_language": "Developed production services in Python",
                        "container_platform": "Operated workloads on Kubernetes",
                    }
                },
            }
        }
    )
    context = AnswerPolicyContext(
        profile=profile,
        profile_version=1,
        selected_cv_id="cv",
        selected_cv_hash="c" * 64,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
    )
    fields = (
        FormFieldV1(
            field_id="language",
            canonical_name=None,
            label="Relevant technical experience",
            field_type="text",
            required=True,
            position=0,
        ),
        FormFieldV1(
            field_id="container",
            canonical_name=None,
            label="Provide one relevant technical experience",
            field_type="text",
            required=True,
            position=1,
        ),
    )

    result = await AnswerPolicyV1(llm_client=SwappingModelClient()).plan_fields(
        fields,
        context,
    )

    assert result.decisions[0].value == "Developed production services in Python"
    assert result.decisions[1].value is None
    assert result.model_digest == _QUALIFIED_MODEL_DIGEST
    assert ReasonCode.LLM_UNAVAILABLE in result.blockers
    assert ReasonCode.REQUIRED_FIELD_UNKNOWN in result.blockers


@pytest.mark.asyncio
@pytest.mark.parametrize("use_valid_but_unrelated_ref", [False, True])
async def test_plausible_llm_answer_without_semantic_evidence_is_not_resolved(
    use_valid_but_unrelated_ref,
):
    class UnsupportedClient:
        model_identity = SimpleNamespace(local=True)

        async def generate_typed(self, **_kwargs):
            return SimpleNamespace(
                value=LLMFieldAnswerV1(
                    value="I have extensive experience with this employer system.",
                    confidence=0.99,
                    evidence_refs=(
                        (f"cv:{'c' * 64}:primary_language",) if use_valid_but_unrelated_ref else ()
                    ),
                ),
                model_identity=SimpleNamespace(
                    provider="ollama",
                    model="qwen2.5:7b",
                    local=True,
                    digest=_QUALIFIED_MODEL_DIGEST,
                ),
            )

    profile = UserProfile.model_validate(
        {
            "evidence": {
                "cv_extracted_by_artifact": {"c" * 64: {"primary_language": "Python"}},
            }
        }
    )
    context = AnswerPolicyContext(
        profile=profile,
        profile_version=1,
        selected_cv_id="cv",
        selected_cv_hash="c" * 64,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
    )
    field = FormFieldV1(
        field_id="employer_code",
        canonical_name="employer_code",
        label="Describe your experience with our internal system.",
        field_type="textarea",
        required=True,
        position=0,
    )

    result = await AnswerPolicyV1(llm_client=UnsupportedClient()).plan_fields(
        (field,),
        context,
    )

    decision = result.decisions[0]
    assert decision.value is None
    assert decision.reason_code == ReasonCode.UNSUPPORTED_CLAIM
    assert ReasonCode.REQUIRED_FIELD_UNKNOWN in result.blockers
    assert ReasonCode.UNSUPPORTED_CLAIM in result.blockers

    now = datetime.now(UTC)
    plan = FormPlanV1(
        plan_id=uuid4(),
        application_id=1,
        application_revision=1,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
        selected_cv_id="cv",
        selected_cv_hash="c" * 64,
        attached_cv_id="cv",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
        profile_version=1,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        fields=(field,),
        decisions=result.decisions,
        blockers=result.blockers,
        llm_prompt_version=result.prompt_version,
        llm_model_provider=result.model_provider,
        llm_model_name=result.model_name,
        llm_model_digest=result.model_digest,
    )
    assert plan.ready_for_permit is False


@pytest.mark.asyncio
async def test_generic_option_value_cannot_cross_cite_an_unrelated_confirmed_fact():
    class CrossCitingClient:
        model_identity = SimpleNamespace(local=True)

        async def generate_typed(self, **_kwargs):
            return SimpleNamespace(
                value=LLMFieldAnswerV1(
                    value="yes",
                    confidence=1.0,
                    evidence_refs=("profile:user_confirmed:remote_preference",),
                ),
                model_identity=SimpleNamespace(
                    provider="ollama",
                    model="qwen2.5:7b",
                    local=True,
                    digest=_QUALIFIED_MODEL_DIGEST,
                ),
            )

    profile = UserProfile.model_validate(
        {"evidence": {"user_confirmed": {"remote_preference": "yes"}}}
    )
    context = AnswerPolicyContext(
        profile=profile,
        profile_version=1,
        selected_cv_id="cv",
        selected_cv_hash="c" * 64,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
    )
    field = FormFieldV1.model_validate(
        {
            "field_id": "team_leadership",
            "canonical_name": "team_leadership",
            "label": "Have you led a team of engineers?",
            "field_type": "radio",
            "required": True,
            "position": 0,
            "options": [
                {"value": "yes", "label": "Yes"},
                {"value": "no", "label": "No"},
            ],
        }
    )

    result = await AnswerPolicyV1(llm_client=CrossCitingClient()).plan_fields(
        (field,),
        context,
    )

    assert result.decisions[0].value is None
    assert result.decisions[0].reason_code == ReasonCode.UNSUPPORTED_CLAIM
    assert ReasonCode.REQUIRED_FIELD_UNKNOWN in result.blockers


@pytest.mark.asyncio
async def test_unscoped_cv_fact_is_never_relabelled_as_the_selected_cv():
    profile = UserProfile.model_validate({"evidence": {"cv_extracted": {"team_leadership": "yes"}}})
    context = AnswerPolicyContext(
        profile=profile,
        profile_version=1,
        selected_cv_id="cv-b",
        selected_cv_hash="b" * 64,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
    )
    field = FormFieldV1.model_validate(
        {
            "field_id": "team_leadership",
            "canonical_name": "team_leadership",
            "label": "Have you led a team of engineers?",
            "field_type": "radio",
            "required": True,
            "position": 0,
            "options": [
                {"value": "yes", "label": "Yes"},
                {"value": "no", "label": "No"},
            ],
        }
    )

    result = await AnswerPolicyV1().plan_fields((field,), context)

    assert result.decisions[0].value is None
    assert result.decisions[0].evidence_refs == ()
    assert result.decisions[0].reason_code == ReasonCode.REQUIRED_FIELD_UNKNOWN
