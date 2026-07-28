"""Two-phase adapter lifecycle and uncertainty boundary coverage."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import submitters.smartrecruiters_v1 as smartrecruiters_v1_module
from core.submission_domain import (
    VERIFIED_ATTACHMENT_EVIDENCE_REF,
    AlreadyAppliedOutcome,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    ConfirmedSubmittedOutcome,
    FailedBeforeCommitOutcome,
    FinalSubmitPermit,
    FormPlanV1,
    NeedsReviewOutcome,
    PreparedFinalActionV1,
    ReasonCode,
    UnknownOutcome,
)
from ingestion.url_utils import normalize_url
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import AdapterPreflightContext, SubmitterRegistry
from submitters.platforms import QualificationTier, adapter_for_platform
from submitters.smartrecruiters_identity import (
    parse_smartrecruiters_candidate_identity,
    resolve_smartrecruiters_posting_identity,
)
from submitters.smartrecruiters_v1 import (
    SmartRecruitersAdapterBlockedError,
    SmartRecruitersAttachmentProof,
    SmartRecruitersBrowserSnapshot,
    SmartRecruitersBrowserV1,
    SmartRecruitersFinalActionAmbiguousError,
    SmartRecruitersFinalActionProof,
    observe_smartrecruiters_v1_disclosures,
    observe_smartrecruiters_v1_fields,
    register_smartrecruiters_browser_v1,
    smartrecruiters_v1_final_action_binding,
    smartrecruiters_v1_form_fingerprint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "smartrecruiters_v1"
JOB_URL = "https://jobs.smartrecruiters.com/FixtureCo/123456789-sanitized-role"
CV_HASH = "a" * 64
NOW = datetime(2026, 7, 27, 10, tzinfo=UTC)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _plan_and_permit(*, cv_hash: str = CV_HASH):
    html = _fixture("application_form.html")
    candidate = parse_smartrecruiters_candidate_identity(JOB_URL)
    identity = resolve_smartrecruiters_posting_identity(html, candidate)
    fields = observe_smartrecruiters_v1_fields(html, identity=identity)
    disclosures = observe_smartrecruiters_v1_disclosures(
        html,
        identity=identity,
    )
    binding = smartrecruiters_v1_final_action_binding(
        html,
        identity=identity,
        fields=fields,
        disclosures=disclosures,
    )
    fingerprint = smartrecruiters_v1_form_fingerprint(
        identity,
        fields,
        disclosures,
        binding,
    )
    decisions = tuple(
        AnswerDecisionV1(
            field_id=field.field_id,
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.USER_CONFIRMED,
            value=(
                True
                if field.field_id == "privacy_consent"
                else (
                    "verified_attachment"
                    if field.field_id == "resume"
                    else (
                        ("decline" if field.field_id == "diversity_choice" else "two")
                        if field.field_type.value == "select"
                        else "Reviewed"
                    )
                )
            ),
            confidence=1,
            evidence_refs=("operator_confirmation:fixture",),
        )
        for field in fields
    )
    # The domain requires exact verified-attachment provenance/sentinel.
    decisions = tuple(
        (
            decision.model_copy(
                update={
                    "provenance": AnswerProvenance.VERIFIED_ATTACHMENT,
                    "evidence_refs": (VERIFIED_ATTACHMENT_EVIDENCE_REF,),
                }
            )
            if decision.field_id == "resume"
            else decision
        )
        for decision in decisions
    )
    plan = FormPlanV1(
        plan_id=uuid4(),
        application_id=1,
        application_revision=1,
        adapter_name="smartrecruiters",
        adapter_version="1.0.0",
        selector_version="smartrecruiters-candidate-v1",
        form_fingerprint=fingerprint,
        selected_cv_id="fixture-cv",
        selected_cv_hash=cv_hash,
        attached_cv_id="fixture-cv",
        attached_cv_hash=cv_hash,
        attachment_verified=True,
        profile_version=1,
        session_verified_at=NOW,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        fields=fields,
        disclosures=disclosures,
        decisions=decisions,
    )
    permit = FinalSubmitPermit(
        attempt_id=1,
        job_url_hash="b" * 64,
        application_revision=1,
        adapter_name="smartrecruiters",
        adapter_version="1.0.0",
        selector_version="smartrecruiters-candidate-v1",
        form_fingerprint=fingerprint,
        cv_hash=cv_hash,
        expires_at=NOW + timedelta(minutes=5),
        nonce="permit-fixture-123456",
    )
    return identity, plan, permit


class _Session:
    def __init__(self, identity, plan, *, click_error=None, confirmation=True) -> None:
        self.identity = identity
        self.plan = plan
        self.click_error = click_error
        self.confirmation = confirmation
        self.closed = False
        self.clicked = 0

    async def navigate(self, _url):
        return None

    async def open_candidate_form(self, _identity):
        return None

    async def snapshot(self):
        name = "verified_confirmation.html" if self.clicked else "application_form.html"
        return SmartRecruitersBrowserSnapshot(_fixture(name), JOB_URL)

    async def ensure_resume_attachment(self, **_kwargs):
        return self._attachment()

    async def verify_resume_attachment(self, **_kwargs):
        return self._attachment()

    def _attachment(self):
        return SmartRecruitersAttachmentProof(
            cv_id=self.plan.selected_cv_id,
            cv_sha256=self.plan.selected_cv_hash,
            upload_complete=True,
            receipt_sha256="c" * 64,
            resume_control_sha256="d" * 64,
        )

    async def fill(self, _decisions):
        return None

    async def prepare_final_action(self, **_kwargs):
        action_url = (
            "https://jobs.smartrecruiters.com/candidate-experience/postings/"
            f"{self.identity.posting_uuid}/applications"
        )
        return SmartRecruitersFinalActionProof(
            identity_sha256=__import__("hashlib")
            .sha256(self.identity.stable_key.encode())
            .hexdigest(),
            action_url_sha256=__import__("hashlib").sha256(action_url.encode()).hexdigest(),
            form_fingerprint=self.plan.form_fingerprint,
            method="POST",
            encoding="multipart/form-data",
            submitter_sha256=__import__("hashlib")
            .sha256(b'button[data-qa="submit-application"][type="submit"]')
            .hexdigest(),
            actionability_sha256="1" * 64,
            disclosures_sha256=__import__(
                "submitters.smartrecruiters_v1",
                fromlist=["smartrecruiters_disclosures_digest"],
            ).smartrecruiters_disclosures_digest(self.plan.disclosures),
            resume_control_sha256="d" * 64,
            attached_cv_sha256=self.plan.selected_cv_hash,
            payload_commitment_sha256="2" * 64,
            user_field_count=len(self.plan.fields),
            disclosure_count=len(self.plan.disclosures),
            precommit_mutation_count=0,
        )

    async def click_final_action(self, _proof):
        self.clicked += 1
        if self.click_error is not None:
            raise self.click_error

    async def confirmation_reference(self, _identity):
        return "fixture-confirmation-001" if self.confirmation else None

    async def close(self):
        self.closed = True


class _TerminalPhaseSession:
    def __init__(self, initial: str, after_open: str | None) -> None:
        self.initial = initial
        self.after_open = after_open
        self.open_calls = 0
        self.closed = False

    async def navigate(self, _url):
        return None

    async def open_candidate_form(self, _identity):
        self.open_calls += 1

    async def snapshot(self):
        name = self.after_open if self.open_calls and self.after_open else self.initial
        return SmartRecruitersBrowserSnapshot(_fixture(name), JOB_URL)

    async def close(self):
        self.closed = True


def _live_descriptor(form_fingerprint: str):
    descriptor = adapter_for_platform("smartrecruiters")
    assert descriptor is not None
    return replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(form_fingerprint,),
    )


def _resume(tmp_path: Path) -> tuple[Path, str]:
    payload = b"%PDF-1.4\nsanitized lifecycle fixture\n%%EOF\n"
    path = tmp_path / "fixture-cv.pdf"
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def _profile() -> dict:
    return {
        "personal": {
            "name": "Fixture Candidate",
            "email": "candidate@example.test",
        }
    }


def _context(path: Path, digest: str) -> AdapterPreflightContext:
    return AdapterPreflightContext(
        normalized_job_url=normalize_url(JOB_URL),
        selected_cv_id="fixture-cv",
        selected_cv_hash=digest,
        resume_path=str(path.resolve()),
    )


_TERMINAL_LIFECYCLE_CASES = (
    ("captcha.html", None, ReasonCode.CHALLENGE_DETECTED, 0),
    ("login.html", None, ReasonCode.SESSION_EXPIRED, 0),
    ("mfa.html", None, ReasonCode.MFA_REQUIRED, 0),
    ("closed_job.html", None, ReasonCode.JOB_CLOSED, 0),
    ("already_applied.html", None, ReasonCode.ALREADY_APPLIED, 0),
    ("candidate_job.html", "captcha.html", ReasonCode.CHALLENGE_DETECTED, 1),
)


@pytest.mark.parametrize(
    ("initial", "after_open", "expected_reason", "expected_resolver_calls"),
    _TERMINAL_LIFECYCLE_CASES,
)
@pytest.mark.asyncio
async def test_inspection_classifies_terminal_pages_before_identity_resolution(
    tmp_path,
    monkeypatch,
    initial,
    after_open,
    expected_reason,
    expected_resolver_calls,
) -> None:
    path, digest = _resume(tmp_path)
    session = _TerminalPhaseSession(initial, after_open)
    resolver_calls = 0
    real_resolver = resolve_smartrecruiters_posting_identity

    def counting_resolver(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return real_resolver(*args, **kwargs)

    monkeypatch.setattr(
        smartrecruiters_v1_module,
        "resolve_smartrecruiters_posting_identity",
        counting_resolver,
    )
    adapter = SmartRecruitersBrowserV1(
        browser_factory=lambda _url: session,
        clock=lambda: NOW,
    )

    with pytest.raises(SmartRecruitersAdapterBlockedError) as raised:
        await adapter.inspect(
            application_id=1,
            application_revision=1,
            job=JobData(title="Fixture role", company="Fixture", apply_url=JOB_URL),
            application=GeneratedApplication(cv_sha256=digest, profile_version=1),
            user_profile=_profile(),
            resume_path=str(path.resolve()),
            selected_cv_id="fixture-cv",
        )

    assert raised.value.reason_code is expected_reason
    assert resolver_calls == expected_resolver_calls
    assert session.open_calls == (1 if after_open else 0)
    assert session.closed is True


@pytest.mark.parametrize(
    ("initial", "after_open", "expected_reason", "expected_resolver_calls"),
    _TERMINAL_LIFECYCLE_CASES,
)
@pytest.mark.asyncio
async def test_preflight_classifies_terminal_pages_before_identity_resolution(
    tmp_path,
    monkeypatch,
    initial,
    after_open,
    expected_reason,
    expected_resolver_calls,
) -> None:
    path, digest = _resume(tmp_path)
    _identity, plan, permit = _plan_and_permit(cv_hash=digest)
    session = _TerminalPhaseSession(initial, after_open)
    resolver_calls = 0
    real_resolver = resolve_smartrecruiters_posting_identity

    def counting_resolver(*args, **kwargs):
        nonlocal resolver_calls
        resolver_calls += 1
        return real_resolver(*args, **kwargs)

    monkeypatch.setattr(
        smartrecruiters_v1_module,
        "resolve_smartrecruiters_posting_identity",
        counting_resolver,
    )
    adapter = SmartRecruitersBrowserV1(
        browser_factory=lambda _url: session,
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    try:
        outcome = await adapter.preflight(
            plan=plan,
            permit=permit,
            context=_context(path, digest),
        )
    finally:
        await adapter.cleanup_prepared_action(action=None)

    if expected_reason is ReasonCode.ALREADY_APPLIED:
        assert isinstance(outcome, AlreadyAppliedOutcome)
    else:
        assert isinstance(outcome, NeedsReviewOutcome)
    assert outcome.reason_code is expected_reason
    assert resolver_calls == expected_resolver_calls
    assert session.open_calls == (1 if after_open else 0)
    assert session.closed is True


def test_default_descriptor_registers_inspector_but_never_enables_execution() -> None:
    registry = SubmitterRegistry()
    adapter = register_smartrecruiters_browser_v1(
        registry,
        browser_factory=lambda _url: pytest.fail("browser must stay lazy"),
    )
    job = JobData(title="Fixture", apply_url=JOB_URL)

    assert adapter.descriptor.qualification is QualificationTier.FIXTURE_QUALIFIED
    assert adapter.descriptor.qualified_form_scope == ()
    assert adapter.descriptor.allows_final_execution is False
    assert adapter.can_inspect(job)
    assert registry.get_inspector(job) is None
    assert (
        registry.get_final_executor(
            job,
            adapter_version="1.0.0",
            selector_version="smartrecruiters-candidate-v1",
            execution_contract_version="two-phase-v2",
            form_fingerprint="f" * 64,
        )
        is None
    )


def test_form_plan_rejects_disclosures_bound_to_unobserved_consent() -> None:
    _identity_value, plan, _permit = _plan_and_permit()
    payload = plan.model_dump(mode="json")
    payload["disclosures"][0]["acknowledgement_field_id"] = "missing-consent"

    with pytest.raises(ValueError, match="disclosure acknowledgement"):
        FormPlanV1.model_validate(payload)


@pytest.mark.asyncio
async def test_commit_confirms_only_exact_fresh_visible_employer_evidence() -> None:
    identity, plan, permit = _plan_and_permit()
    session = _Session(identity, plan)
    adapter = SmartRecruitersBrowserV1(
        browser_factory=lambda _url: session,
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )
    action = PreparedFinalActionV1(
        attempt_id=permit.attempt_id,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        attached_cv_hash=plan.attached_cv_hash,
        prepared_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        action_nonce="3" * 64,
    )
    proof = await session.prepare_final_action()
    adapter._prepared[action.action_nonce] = __import__(
        "submitters.smartrecruiters_v1",
        fromlist=["_PreparedState"],
    )._PreparedState(
        session=session,
        plan=plan,
        permit=permit,
        identity=identity,
        proof=proof,
        pre_action_html=_fixture("application_form.html"),
    )

    result = await adapter.commit(action=action, permit=permit)

    assert isinstance(result, ConfirmedSubmittedOutcome)
    assert session.clicked == 1


@pytest.mark.asyncio
async def test_final_action_proof_binds_the_exact_native_candidate_endpoint() -> None:
    identity, plan, _permit = _plan_and_permit()
    session = _Session(identity, plan)
    proof = await session.prepare_final_action()

    assert proof.valid_for(identity=identity, plan=plan)
    assert not replace(proof, action_url_sha256="0" * 64).valid_for(
        identity=identity,
        plan=plan,
    )
    assert not replace(proof, submitter_sha256="0" * 64).valid_for(
        identity=identity,
        plan=plan,
    )


@pytest.mark.asyncio
async def test_possible_send_without_confirmation_is_unknown_and_not_replayed() -> None:
    identity, plan, permit = _plan_and_permit()
    session = _Session(
        identity,
        plan,
        click_error=SmartRecruitersFinalActionAmbiguousError("possible send"),
    )
    adapter = SmartRecruitersBrowserV1(
        browser_factory=lambda _url: session,
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )
    action = PreparedFinalActionV1(
        attempt_id=permit.attempt_id,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        attached_cv_hash=plan.attached_cv_hash,
        prepared_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        action_nonce="4" * 64,
    )
    proof = await session.prepare_final_action()
    prepared_type = __import__(
        "submitters.smartrecruiters_v1",
        fromlist=["_PreparedState"],
    )._PreparedState
    adapter._prepared[action.action_nonce] = prepared_type(
        session=session,
        plan=plan,
        permit=permit,
        identity=identity,
        proof=proof,
        pre_action_html=_fixture("application_form.html"),
    )

    first = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)

    assert isinstance(first, UnknownOutcome)
    assert first.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert isinstance(replay, FailedBeforeCommitOutcome)
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.clicked == 1


@pytest.mark.asyncio
async def test_proven_pre_send_block_is_failed_before_commit_not_unknown() -> None:
    identity, plan, permit = _plan_and_permit()
    session = _Session(
        identity,
        plan,
        click_error=SmartRecruitersAdapterBlockedError(ReasonCode.FORM_CHANGED),
    )
    adapter = SmartRecruitersBrowserV1(
        browser_factory=lambda _url: session,
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )
    action = PreparedFinalActionV1(
        attempt_id=permit.attempt_id,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        attached_cv_hash=plan.attached_cv_hash,
        prepared_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        action_nonce="5" * 64,
    )
    proof = await session.prepare_final_action()
    prepared_type = __import__(
        "submitters.smartrecruiters_v1",
        fromlist=["_PreparedState"],
    )._PreparedState
    adapter._prepared[action.action_nonce] = prepared_type(
        session=session,
        plan=plan,
        permit=permit,
        identity=identity,
        proof=proof,
        pre_action_html=_fixture("application_form.html"),
    )

    result = await adapter.commit(action=action, permit=permit)

    assert isinstance(result, FailedBeforeCommitOutcome)
    assert result.reason_code is ReasonCode.FORM_CHANGED
