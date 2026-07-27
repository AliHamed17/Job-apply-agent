"""Fail-closed tests for two-phase adapters and employer evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from core.submission_domain import FinalSubmitPermit, FormPlanV1
from ingestion.url_utils import normalize_url, url_hash
from jobs.models import JobData
from submitters.base import SubmitterRegistry, two_phase_registry
from submitters.confirmation import (
    AdapterEvidenceRule,
    EvidenceChannel,
    SubmissionEvidenceExpectation,
    SubmissionEvidenceObservation,
    browser_submission_result,
    verify_submission_evidence,
)
from submitters.greenhouse import GreenhouseSubmitter
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    QualificationTier,
    adapter_for_platform,
    registered_adapters,
)


def _expectation() -> SubmissionEvidenceExpectation:
    return SubmissionEvidenceExpectation(
        attempt_id=42,
        platform="greenhouse",
        adapter_version="2.0.0",
        selector_version="greenhouse-v2",
        form_fingerprint="a" * 64,
        attached_cv_hash="b" * 64,
        attachment_verified=True,
        post_action_nonce="single-use-nonce",
        final_action_at=datetime(2026, 7, 26, 10, 0, tzinfo=UTC),
        allowed_rules=(
            AdapterEvidenceRule(
                rule_id="greenhouse-v2:receipt",
                channel=EvidenceChannel.API_RECEIPT,
            ),
            AdapterEvidenceRule(
                rule_id="greenhouse-v2:visible",
                channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
                visible_selector="[data-ats-confirmation]",
            ),
        ),
    )


def _observation(**overrides) -> SubmissionEvidenceObservation:
    values = {
        "attempt_id": 42,
        "platform": "greenhouse",
        "adapter_version": "2.0.0",
        "selector_version": "greenhouse-v2",
        "form_fingerprint": "a" * 64,
        "attached_cv_hash": "b" * 64,
        "post_action_nonce": "single-use-nonce",
        "rule_id": "greenhouse-v2:receipt",
        "channel": EvidenceChannel.API_RECEIPT,
        "evidence_reference": "application-123",
        "observed_at": datetime(2026, 7, 26, 10, 1, tzinfo=UTC),
        "observed_after_final_action": True,
        "was_present_before_action": False,
        "response_status": 201,
        "response_schema_valid": True,
    }
    values.update(overrides)
    return SubmissionEvidenceObservation(**values)


@pytest.mark.parametrize(
    ("overrides", "reason_code"),
    [
        ({"attempt_id": 43}, "EVIDENCE_BINDING_MISMATCH"),
        ({"form_fingerprint": "c" * 64}, "EVIDENCE_BINDING_MISMATCH"),
        ({"attached_cv_hash": "d" * 64}, "EVIDENCE_BINDING_MISMATCH"),
        ({"post_action_nonce": "replayed"}, "EVIDENCE_BINDING_MISMATCH"),
        ({"rule_id": "generic:success"}, "EVIDENCE_BINDING_MISMATCH"),
        (
            {"channel": EvidenceChannel.EMPLOYER_APPLICATION_ID},
            "EVIDENCE_BINDING_MISMATCH",
        ),
        (
            {
                "rule_id": "greenhouse-v2:visible",
                "channel": EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
                "visible_selector": "h1",
                "computed_visible": True,
            },
            "EVIDENCE_BINDING_MISMATCH",
        ),
        ({"observed_after_final_action": False}, "EVIDENCE_NOT_POST_ACTION"),
        (
            {"observed_at": datetime(2026, 7, 26, 9, 59, tzinfo=UTC)},
            "EVIDENCE_TIMESTAMP_INVALID",
        ),
        ({"was_present_before_action": True}, "EVIDENCE_PREEXISTED"),
        ({"evidence_reference": "  "}, "EVIDENCE_REFERENCE_MISSING"),
        ({"response_status": 200, "response_schema_valid": False}, "API_RECEIPT_INVALID"),
        ({"response_status": 500, "response_schema_valid": True}, "API_RECEIPT_INVALID"),
    ],
)
def test_api_evidence_fails_closed(overrides, reason_code):
    result = verify_submission_evidence(_expectation(), _observation(**overrides))

    assert result.confirmed is False
    assert result.reason_code == reason_code


def test_schema_valid_receipt_is_bound_to_exact_attempt_form_and_cv():
    result = browser_submission_result(
        platform="greenhouse",
        page_url="https://boards.greenhouse.io/acme/thank-you",
        html="",
        expectation=_expectation(),
        observation=_observation(),
    )

    assert result.success is True
    assert result.status == "submitted"
    assert result.reason_code == "EMPLOYER_VERIFIED"
    assert result.confirmation_id == "application-123"
    assert len(result.diagnostic_details["evidence_digest"]) == 64


def test_unverified_attachment_can_never_produce_employer_evidence():
    result = verify_submission_evidence(
        replace(_expectation(), attachment_verified=False),
        _observation(),
    )

    assert result.confirmed is False
    assert result.reason_code == "EVIDENCE_BINDING_MISMATCH"


def test_hidden_visible_confirmation_is_not_evidence():
    observation = _observation(
        rule_id="greenhouse-v2:visible",
        channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
        visible_selector="[data-ats-confirmation]",
        computed_visible=True,
        response_status=None,
        response_schema_valid=False,
    )
    result = verify_submission_evidence(
        _expectation(),
        observation,
        pre_action_html="<main><form>Apply</form></main>",
        post_action_html=(
            '<main><div data-ats-confirmation hidden data-id="application-123">'
            "Application submitted</div></main>"
        ),
    )

    assert result.confirmed is False
    assert result.reason_code == "VISIBLE_EVIDENCE_MISSING"


def test_adapter_computed_hidden_confirmation_is_not_evidence():
    observation = _observation(
        rule_id="greenhouse-v2:visible",
        channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
        visible_selector="[data-ats-confirmation]",
        computed_visible=False,
        response_status=None,
        response_schema_valid=False,
    )
    result = verify_submission_evidence(
        _expectation(),
        observation,
        pre_action_html="<main><form>Apply</form></main>",
        post_action_html=(
            "<main><div class='offscreen' data-ats-confirmation>Application submitted</div></main>"
        ),
    )

    assert result.confirmed is False
    assert result.reason_code == "VISIBLE_EVIDENCE_HIDDEN"


def test_preexisting_confirmation_markup_is_not_evidence_even_if_hidden_before():
    observation = _observation(
        rule_id="greenhouse-v2:visible",
        channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
        visible_selector="[data-ats-confirmation]",
        computed_visible=True,
        response_status=None,
        response_schema_valid=False,
    )
    result = verify_submission_evidence(
        _expectation(),
        observation,
        pre_action_html=(
            "<div data-ats-confirmation style='display:none'>Application submitted</div>"
        ),
        post_action_html="<div data-ats-confirmation>Application submitted</div>",
    )

    assert result.confirmed is False
    assert result.reason_code == "EVIDENCE_PREEXISTED"


def test_new_adapter_specific_visible_confirmation_can_be_verified():
    observation = _observation(
        rule_id="greenhouse-v2:visible",
        channel=EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION,
        visible_selector="[data-ats-confirmation]",
        computed_visible=True,
        response_status=None,
        response_schema_valid=False,
    )
    result = verify_submission_evidence(
        _expectation(),
        observation,
        pre_action_html="<main><form>Apply</form></main>",
        post_action_html=(
            "<main><div data-ats-confirmation='application-123'>Application submitted</div></main>"
        ),
    )

    assert result.confirmed is True
    assert result.evidence is not None
    assert result.evidence.evidence_type is EvidenceChannel.VISIBLE_POST_CLICK_CONFIRMATION


class _TwoPhaseAdapter:
    def __init__(self, descriptor):
        self.descriptor = descriptor

    def can_inspect(self, job):
        return bool(job.apply_url)

    async def inspect(self, **_kwargs):
        return None

    async def preflight(self, **_kwargs):
        return None

    async def commit(self, **_kwargs):
        return None


def _job() -> JobData:
    return JobData(
        title="Safety Engineer",
        apply_url="https://boards.greenhouse.io/acme/jobs/1",
    )


def test_current_adapters_have_no_live_final_execution_scope():
    registry = SubmitterRegistry()

    for descriptor in registered_adapters():
        assert descriptor.allows_live_submission is False
        assert descriptor.allows_final_execution is False
        assert descriptor.qualified_form_scope == ()
        if descriptor.platform in {"workday", "greenhouse", "lever"}:
            assert descriptor.execution_contract_version == TWO_PHASE_EXECUTION_CONTRACT_VERSION
            assert descriptor.qualification is QualificationTier.FIXTURE_QUALIFIED
        else:
            assert descriptor.execution_contract_version is None

    descriptor = adapter_for_platform("greenhouse")
    assert descriptor is not None
    registry.register_two_phase(_TwoPhaseAdapter(descriptor))
    assert registry.get_inspector(_job()) is None
    assert (
        registry.get_final_executor(
            _job(),
            adapter_version=descriptor.adapter_version,
            selector_version=descriptor.selector_version,
            execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
            form_fingerprint="anything",
        )
        is None
    )
    assert (
        two_phase_registry.get_final_executor(
            _job(),
            adapter_version=descriptor.adapter_version,
            selector_version=descriptor.selector_version,
            execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
            form_fingerprint="anything",
        )
        is None
    )


def test_legacy_adapter_cannot_enter_two_phase_registry():
    registry = SubmitterRegistry()

    with pytest.raises(TypeError):
        registry.register_two_phase(GreenhouseSubmitter())


def test_final_executor_requires_every_pinned_version_and_exact_form_scope():
    current = adapter_for_platform("greenhouse")
    assert current is not None
    qualified = replace(
        current,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
        qualified_form_scope=("qualified-form",),
    )
    adapter = _TwoPhaseAdapter(qualified)
    registry = SubmitterRegistry()

    with (
        patch("submitters.base.adapter_for_platform", return_value=qualified),
        patch("submitters.base.adapter_for_url", return_value=qualified),
    ):
        registry.register_two_phase(adapter)
        assert (
            registry.get_final_executor(
                _job(),
                adapter_version=qualified.adapter_version,
                selector_version=qualified.selector_version,
                execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
                form_fingerprint="qualified-form",
            )
            is adapter
        )
        assert (
            registry.get_final_executor(
                _job(),
                adapter_version=qualified.adapter_version,
                selector_version="selector-drift",
                execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
                form_fingerprint="qualified-form",
            )
            is None
        )
        assert (
            registry.get_final_executor(
                _job(),
                adapter_version=qualified.adapter_version,
                selector_version=qualified.selector_version,
                execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
                form_fingerprint="unqualified-form",
            )
            is None
        )


def test_strong_executor_resolution_requires_bound_unexpired_plan_and_permit():
    current = adapter_for_platform("greenhouse")
    assert current is not None
    fingerprint = "a" * 64
    cv_hash = "b" * 64
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    qualified = replace(
        current,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
        qualified_form_scope=(fingerprint,),
    )
    plan = FormPlanV1(
        plan_id=uuid4(),
        application_id=12,
        application_revision=3,
        adapter_name=qualified.platform,
        adapter_version=qualified.adapter_version,
        selector_version=qualified.selector_version,
        form_fingerprint=fingerprint,
        selected_cv_id="ai-cv",
        selected_cv_hash=cv_hash,
        attached_cv_id="ai-cv",
        attached_cv_hash=cv_hash,
        attachment_verified=True,
        profile_version=2,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        fields=(),
        decisions=(),
    )
    permit = FinalSubmitPermit(
        attempt_id=42,
        job_url_hash=url_hash(normalize_url(_job().apply_url)),
        application_revision=plan.application_revision,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        cv_hash=plan.selected_cv_hash,
        expires_at=now + timedelta(minutes=5),
        nonce="one-use-nonce",
    )
    adapter = _TwoPhaseAdapter(qualified)
    registry = SubmitterRegistry()

    with (
        patch("submitters.base.adapter_for_platform", return_value=qualified),
        patch("submitters.base.adapter_for_url", return_value=qualified),
    ):
        registry.register_two_phase(adapter)
        assert (
            registry.resolve_final_executor(
                _job(),
                plan,
                permit,
                TWO_PHASE_EXECUTION_CONTRACT_VERSION,
                now,
            )
            is adapter
        )
        assert (
            registry.resolve_final_executor(
                _job(),
                plan,
                permit,
                TWO_PHASE_EXECUTION_CONTRACT_VERSION,
                now + timedelta(minutes=6),
            )
            is None
        )
        drifted_permit = FinalSubmitPermit(
            **{
                **permit.model_dump(),
                "selector_version": "selector-drift",
            }
        )
        assert (
            registry.resolve_final_executor(
                _job(),
                plan,
                drifted_permit,
                TWO_PHASE_EXECUTION_CONTRACT_VERSION,
                now,
            )
            is None
        )
