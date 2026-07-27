"""Fail-closed two-phase contract tests for GreenhouseBrowserV1."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.submission_domain import (
    AttemptOutcome,
    FinalSubmitPermit,
    PreparedFinalActionV1,
    ReasonCode,
    UnknownOutcome,
)
from ingestion.url_utils import normalize_url
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import AdapterPreflightContext
from submitters.greenhouse_v1 import (
    GREENHOUSE_V1_ADAPTER_VERSION,
    GREENHOUSE_V1_NATIVE_TRANSPORT,
    GREENHOUSE_V1_SELECTOR_VERSION,
    GreenhouseAdapterBlockedError,
    GreenhouseAnswerBinding,
    GreenhouseAtomicCommitExpectation,
    GreenhouseAtomicCommitObservation,
    GreenhouseAttachmentProof,
    GreenhouseBrowserSnapshot,
    GreenhouseBrowserV1,
    GreenhousePayloadBinding,
    GreenhouseReviewedAnswerBinding,
    _atomic_observation_outcome,
    greenhouse_v1_dom_commitment,
    greenhouse_visible_confirmation_digest,
)
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    QualificationTier,
    adapter_for_platform,
)

FIXTURES = Path(__file__).parent / "fixtures" / "greenhouse_v1"
JOB_URL = "https://job-boards.greenhouse.io/fixture/jobs/123456"
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
ACTION_BINDING = "e" * 64
PAYLOAD_COMMITMENT = "d" * 64


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fixture_descriptor():
    current = adapter_for_platform("greenhouse")
    assert current is not None
    return replace(
        current,
        adapter_version=GREENHOUSE_V1_ADAPTER_VERSION,
        selector_version=GREENHOUSE_V1_SELECTOR_VERSION,
        transport="browser",
        authentication_mode="public_candidate_flow",
        qualification=QualificationTier.FIXTURE_QUALIFIED,
        qualified_form_scope=(),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    )


def _live_descriptor(fingerprint: str):
    return replace(
        _fixture_descriptor(),
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(fingerprint,),
    )


class _FakeSession:
    def __init__(
        self,
        *,
        forms: tuple[str, ...] | None = None,
        initial: str = "embedded_form.html",
        post_click: str = "verified_confirmation.html",
        confirmation_visible: bool = True,
        click_error: bool = False,
        mutate_path: Path | None = None,
        action_binding: str = ACTION_BINDING,
        post_click_url: str = JOB_URL,
        atomic_drift: str | None = None,
        request_url: str = JOB_URL,
        confirmation_reference_override: str | None = None,
    ) -> None:
        self.forms = list(forms or (_fixture(initial),))
        self.index = 0
        self.html = self.forms[0]
        self.post_click_html = _fixture(post_click)
        self.confirmation_visible = confirmation_visible
        self.click_error = click_error
        self.mutate_path = mutate_path
        self.action_binding = action_binding
        self.url = JOB_URL
        self.post_click_url = post_click_url
        self.atomic_drift = atomic_drift
        self.request_url = request_url
        self.confirmation_reference_override = confirmation_reference_override
        self.navigated = False
        self.opened = False
        self.closed = False
        self.clicked = 0
        self.final_action_invocations = 0
        self.filled: list[tuple] = []
        self.uploaded_bytes: bytes | None = None
        self.attachment: GreenhouseAttachmentProof | None = None

    async def navigate(self, _url: str) -> None:
        self.navigated = True
        if self.mutate_path is not None:
            self.mutate_path.write_bytes(b"%PDF-1.4\nchanged after verified read\n%%EOF\n")

    async def open_candidate_form(self) -> None:
        self.opened = True

    async def snapshot(self) -> GreenhouseBrowserSnapshot:
        return GreenhouseBrowserSnapshot(html=self.html, url=self.url, locale="en")

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> GreenhouseAttachmentProof:
        self.uploaded_bytes = bytes(resume_bytes)
        valid = hashlib.sha256(resume_bytes).hexdigest() == expected_sha256
        self.attachment = GreenhouseAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=valid,
            receipt_sha256=("f" * 64 if valid else None),
        )
        return self.attachment

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> GreenhouseAttachmentProof:
        return self.attachment or GreenhouseAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=False,
        )

    async def fill(self, decisions: tuple) -> None:
        self.filled.append(decisions)

    async def settle_reversible_form(self) -> None:
        if self.index + 1 < len(self.forms):
            self.index += 1
            self.html = self.forms[self.index]

    async def final_action_ready(self) -> bool:
        return 'type="submit"' in self.html

    async def observed_form_action_binding(self) -> str | None:
        return self.action_binding if 'type="submit"' in self.html else None

    async def final_action_binding(self) -> str | None:
        return self.action_binding if await self.final_action_ready() else None

    async def final_action_url(self) -> str | None:
        return JOB_URL if await self.final_action_ready() else None

    async def commit_dom_commitment(self) -> str | None:
        return greenhouse_v1_dom_commitment(self.html)

    async def commit_payload_binding(
        self,
        *,
        reviewed_answers: tuple[GreenhouseReviewedAnswerBinding, ...],
        expected_cv_sha256: str,
    ) -> GreenhousePayloadBinding | None:
        if (
            not await self.final_action_ready()
            or self.attachment is None
            or self.attachment.cv_sha256 != expected_cv_sha256
        ):
            return None
        answer_bindings = tuple(
            GreenhouseAnswerBinding(
                reviewed=reviewed,
                control_name_sha256=hashlib.sha256(reviewed.field_id.encode("utf-8")).hexdigest(),
            )
            for reviewed in reviewed_answers
        )
        file_binding = next(
            binding for binding in answer_bindings if binding.reviewed.field_type.value == "file"
        )
        return GreenhousePayloadBinding(
            payload_commitment=PAYLOAD_COMMITMENT,
            answer_bindings=answer_bindings,
            resume_control_name_sha256=file_binding.control_name_sha256,
            submitter_binding=None,
        )

    async def atomic_commit(
        self,
        expectation: GreenhouseAtomicCommitExpectation,
    ) -> GreenhouseAtomicCommitObservation:
        self.clicked += 1
        if self.atomic_drift == "evaluate_exception_after_invocation":
            self.final_action_invocations += 1
            raise RuntimeError("synthetic evaluation context loss after invocation")
        request_may_have_left = True
        reason_code = None
        observed_fields = expectation.fields
        observed_answer_bindings = expectation.answer_bindings
        observed_action_binding = expectation.action_binding
        observed_dom_commitment = expectation.dom_commitment
        observed_receipt = expectation.cv_receipt_sha256
        final_action_invoked = True
        if self.atomic_drift == "attachment":
            self.attachment = GreenhouseAttachmentProof(
                cv_id=expectation.cv_id,
                cv_sha256=expectation.cv_sha256,
                upload_complete=False,
            )
            request_may_have_left = False
            observed_receipt = "0" * 64
            reason_code = ReasonCode.ATTACHMENT_UNVERIFIED
            final_action_invoked = False
        elif self.atomic_drift == "field":
            request_may_have_left = False
            observed_fields = tuple(reversed(expectation.fields))
            observed_answer_bindings = tuple(reversed(expectation.answer_bindings))
            reason_code = ReasonCode.FORM_CHANGED
            final_action_invoked = False
        elif self.atomic_drift == "action":
            request_may_have_left = False
            observed_action_binding = "0" * 64
            reason_code = ReasonCode.FORM_CHANGED
            final_action_invoked = False
        elif self.atomic_drift == "invoked_without_request":
            request_may_have_left = False
            reason_code = ReasonCode.FINAL_ACTION_UNCONFIRMED
        elif self.request_url != JOB_URL:
            request_may_have_left = False
            reason_code = ReasonCode.FORM_CHANGED
        if final_action_invoked:
            self.final_action_invocations += 1
        if self.click_error:
            reason_code = ReasonCode.FINAL_ACTION_UNCONFIRMED
        elif request_may_have_left:
            self.html = self.post_click_html
            self.url = self.post_click_url
        return GreenhouseAtomicCommitObservation(
            expected_hostname=expectation.expected_hostname,
            expected_identity=expectation.expected_identity,
            fields=observed_fields,
            variant=expectation.variant,
            form_fingerprint=expectation.form_fingerprint,
            action_binding=observed_action_binding,
            dom_commitment=observed_dom_commitment,
            resolved_action_url=expectation.resolved_action_url,
            native_transport=GREENHOUSE_V1_NATIVE_TRANSPORT,
            payload_commitment=expectation.payload_commitment,
            answer_bindings=observed_answer_bindings,
            resume_control_name_sha256=expectation.resume_control_name_sha256,
            submitter_binding=expectation.submitter_binding,
            cv_id=expectation.cv_id,
            cv_sha256=expectation.cv_sha256,
            cv_receipt_sha256=observed_receipt,
            final_action_invoked=final_action_invoked,
            request_may_have_left=request_may_have_left,
            outbound_request_sha256=(
                hashlib.sha256(self.request_url.encode("utf-8")).hexdigest()
                if request_may_have_left
                else None
            ),
            reason_code=reason_code,
        )

    async def confirmation_reference(self) -> str | None:
        if self.confirmation_reference_override is not None:
            return self.confirmation_reference_override
        if not self.confirmation_visible:
            return None
        if "application-confirmation" not in self.html or "hidden" in self.html:
            return None
        return greenhouse_visible_confirmation_digest(self.html)

    async def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self, session: _FakeSession) -> None:
        self.session = session
        self.calls = 0

    def __call__(self, _url: str) -> _FakeSession:
        self.calls += 1
        return self.session


def _resume(tmp_path: Path) -> tuple[Path, bytes, str]:
    payload = b"%PDF-1.4\nsanitized fixture resume\n%%EOF\n"
    path = tmp_path / "fixture-cv.pdf"
    path.write_bytes(payload)
    return path, payload, hashlib.sha256(payload).hexdigest()


def _profile() -> dict:
    return {
        "personal": {
            "name": "Test Candidate",
            "email": "candidate@example.test",
            "phone": "",
            "location": "",
        }
    }


def _job() -> JobData:
    return JobData(title="Fixture role", company="Fixture", apply_url=JOB_URL)


async def _inspect(
    tmp_path: Path,
    session: _FakeSession,
    *,
    initial: str = "embedded_form.html",
):
    path, payload, digest = _resume(tmp_path)
    if initial != "embedded_form.html":
        session.html = _fixture(initial)
        session.forms = [session.html]
    adapter = GreenhouseBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_fixture_descriptor(),
        clock=lambda: NOW,
    )
    plan = await adapter.inspect(
        application_id=7,
        application_revision=3,
        job=_job(),
        application=GeneratedApplication(
            cv_sha256=digest,
            profile_version=4,
        ),
        user_profile=_profile(),
        resume_path=str(path.resolve()),
        selected_cv_id="fixture-cv",
    )
    return adapter, plan, path, payload, digest


def _permit(plan) -> FinalSubmitPermit:
    return FinalSubmitPermit(
        attempt_id=19,
        job_url_hash="d" * 64,
        application_revision=plan.application_revision,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        cv_hash=plan.selected_cv_hash,
        expires_at=NOW + timedelta(minutes=5),
        nonce="one-use-permit",
    )


def _context(path: Path, digest: str) -> AdapterPreflightContext:
    return AdapterPreflightContext(
        normalized_job_url=normalize_url(JOB_URL),
        selected_cv_id="fixture-cv",
        selected_cv_hash=digest,
        resume_path=str(path.resolve()),
    )


@pytest.mark.asyncio
async def test_inspection_builds_ready_plan_without_final_action(tmp_path) -> None:
    session = _FakeSession()

    _adapter, plan, _path, _payload, _digest = await _inspect(tmp_path, session)

    assert plan.ready_for_permit is True
    assert plan.adapter_version == GREENHOUSE_V1_ADAPTER_VERSION
    assert plan.selector_version == GREENHOUSE_V1_SELECTOR_VERSION
    assert plan.selected_cv_id == plan.attached_cv_id == "fixture-cv"
    assert plan.selected_cv_hash == plan.attached_cv_hash
    assert plan.attachment_verified is True
    assert plan.blockers == ()
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_required_compliance_and_consent_return_partial_review_plan(tmp_path) -> None:
    session = _FakeSession(initial="compliance_consent.html")

    _adapter, plan, _path, _payload, _digest = await _inspect(
        tmp_path,
        session,
        initial="compliance_consent.html",
    )

    assert plan.ready_for_permit is False
    assert ReasonCode.REQUIRED_FIELD_UNKNOWN in plan.blockers
    assert ReasonCode.FORM_PLAN_INCOMPLETE in plan.blockers
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_inspection_uses_bytes_verified_before_path_mutation(tmp_path) -> None:
    path, original, digest = _resume(tmp_path)
    session = _FakeSession(mutate_path=path)
    adapter = GreenhouseBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_fixture_descriptor(),
        clock=lambda: NOW,
    )

    plan = await adapter.inspect(
        application_id=7,
        application_revision=3,
        job=_job(),
        application=GeneratedApplication(cv_sha256=digest, profile_version=4),
        user_profile=_profile(),
        resume_path=str(path.resolve()),
        selected_cv_id="fixture-cv",
    )

    assert plan.attachment_verified is True
    assert session.uploaded_bytes == original
    assert hashlib.sha256(path.read_bytes()).hexdigest() != digest


@pytest.mark.asyncio
async def test_existing_application_state_never_becomes_new_submission(tmp_path) -> None:
    path, _payload, digest = _resume(tmp_path)
    session = _FakeSession(initial="already_applied.html")
    adapter = GreenhouseBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_fixture_descriptor(),
        clock=lambda: NOW,
    )

    with pytest.raises(GreenhouseAdapterBlockedError) as raised:
        await adapter.inspect(
            application_id=7,
            application_revision=3,
            job=_job(),
            application=GeneratedApplication(cv_sha256=digest, profile_version=4),
            user_profile=_profile(),
            resume_path=str(path.resolve()),
            selected_cv_id="fixture-cv",
        )

    assert raised.value.reason_code is ReasonCode.ALREADY_APPLIED
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_fixture_qualified_adapter_cannot_prepare_or_commit(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, path, _payload, digest = await _inspect(tmp_path, inspect_session)
    fixture_adapter = GreenhouseBrowserV1(
        browser_factory=_Factory(_FakeSession()),
        descriptor=_fixture_descriptor(),
        clock=lambda: NOW,
    )

    preflight = await fixture_adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(path, digest),
    )

    assert preflight.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert preflight.reason_code is ReasonCode.ADAPTER_NOT_QUALIFIED


@pytest.mark.asyncio
async def test_preflight_rejects_conditional_drift_after_reversible_fill(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, path, _payload, digest = await _inspect(tmp_path, inspect_session)
    session = _FakeSession(
        forms=(
            _fixture("embedded_form.html"),
            _fixture("embedded_form.html"),
            _fixture("conditional_expanded.html"),
        )
    )
    live_adapter = GreenhouseBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    outcome = await live_adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(path, digest),
    )
    await live_adapter.cleanup_prepared_action(action=None)

    assert outcome.kind is AttemptOutcome.NEEDS_REVIEW
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_preflight_rejects_same_origin_redirect_to_another_job(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, path, _payload, digest = await _inspect(tmp_path, inspect_session)
    session = _FakeSession()
    session.url = "https://job-boards.greenhouse.io/fixture/jobs/999999"
    live_adapter = GreenhouseBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    outcome = await live_adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(path, digest),
    )
    await live_adapter.cleanup_prepared_action(action=None)

    assert outcome.kind is AttemptOutcome.NEEDS_REVIEW
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_preflight_rejects_form_action_changed_since_review(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, path, _payload, digest = await _inspect(tmp_path, inspect_session)
    session = _FakeSession(action_binding="9" * 64)
    live_adapter = GreenhouseBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    outcome = await live_adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(path, digest),
    )
    await live_adapter.cleanup_prepared_action(action=None)

    assert outcome.kind is AttemptOutcome.NEEDS_REVIEW
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert session.clicked == 0
    assert session.closed is True


async def _prepared_live_action(
    tmp_path: Path,
    session: _FakeSession,
    *,
    inspect_fixture: str = "embedded_form.html",
):
    inspect_session = _FakeSession(initial=inspect_fixture)
    _adapter, plan, path, _payload, digest = await _inspect(
        tmp_path,
        inspect_session,
        initial=inspect_fixture,
    )
    live_adapter = GreenhouseBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )
    permit = _permit(plan)
    action = await live_adapter.preflight(
        plan=plan,
        permit=permit,
        context=_context(path, digest),
    )
    assert isinstance(action, PreparedFinalActionV1)
    assert session.clicked == 0
    return live_adapter, action, permit


@pytest.mark.asyncio
async def test_live_descriptor_simulation_clicks_once_and_requires_evidence(tmp_path) -> None:
    session = _FakeSession()
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.CONFIRMED_SUBMITTED
    assert outcome.evidence.evidence_type.value == "visible_post_click_confirmation"
    assert replay.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.clicked == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_precommit_injected_visible_confirmation_stops_before_click(tmp_path) -> None:
    session = _FakeSession()
    adapter, action, permit = await _prepared_live_action(tmp_path, session)
    session.html = _fixture("verified_confirmation.html")

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.NEEDS_REVIEW
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_precommit_form_fingerprint_drift_stops_before_click(tmp_path) -> None:
    session = _FakeSession()
    adapter, action, permit = await _prepared_live_action(tmp_path, session)
    session.html = _fixture("conditional_expanded.html")

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.NEEDS_REVIEW
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_precommit_attachment_invalidation_stops_before_click(tmp_path) -> None:
    session = _FakeSession()
    adapter, action, permit = await _prepared_live_action(tmp_path, session)
    session.attachment = GreenhouseAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256=action.attached_cv_hash,
        upload_complete=False,
    )

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.NEEDS_REVIEW
    assert outcome.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_precommit_form_action_binding_drift_stops_before_click(tmp_path) -> None:
    session = _FakeSession()
    adapter, action, permit = await _prepared_live_action(tmp_path, session)
    session.action_binding = "9" * 64

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.NEEDS_REVIEW
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert session.clicked == 0
    assert session.closed is True


@pytest.mark.parametrize(
    ("atomic_drift", "expected_reason"),
    [
        ("attachment", ReasonCode.ATTACHMENT_UNVERIFIED),
        ("field", ReasonCode.FORM_CHANGED),
        ("action", ReasonCode.FORM_CHANGED),
    ],
)
@pytest.mark.asyncio
async def test_atomic_commit_rejects_attachment_field_or_action_drift_before_request(
    tmp_path,
    atomic_drift,
    expected_reason,
) -> None:
    session = _FakeSession(atomic_drift=atomic_drift)
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.NEEDS_REVIEW
    assert outcome.reason_code is expected_reason
    assert session.clicked == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_wrong_job_gate_after_final_invocation_is_unknown_and_not_retried(
    tmp_path,
) -> None:
    session = _FakeSession(
        request_url="https://job-boards.greenhouse.io/fixture/jobs/999999",
    )
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert replay.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.clicked == 1
    assert session.final_action_invocations == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_intrinsic_invocation_without_gate_event_is_unknown_and_not_retried(
    tmp_path,
) -> None:
    session = _FakeSession(atomic_drift="invoked_without_request")
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert replay.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.clicked == 1
    assert session.final_action_invocations == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_evaluate_exception_after_invocation_is_unknown_and_not_retried(
    tmp_path,
) -> None:
    session = _FakeSession(atomic_drift="evaluate_exception_after_invocation")
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert replay.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.clicked == 1
    assert session.final_action_invocations == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_atomic_observation_mapping_is_total_for_every_reason_and_stage(
    tmp_path,
) -> None:
    session = _FakeSession()
    adapter, action, _permit = await _prepared_live_action(tmp_path, session)
    prepared = adapter._prepared[action.action_nonce]
    expectation = prepared.atomic_expectation
    request_left = await session.atomic_commit(expectation)

    assert _atomic_observation_outcome(request_left, expectation) is None
    for reason_code in ReasonCode:
        observed_after_request = replace(request_left, reason_code=reason_code)
        after_request = _atomic_observation_outcome(
            observed_after_request,
            expectation,
        )
        assert after_request is not None
        assert after_request.kind is AttemptOutcome.UNKNOWN

        invoked_without_request = replace(
            request_left,
            final_action_invoked=True,
            request_may_have_left=False,
            outbound_request_sha256=None,
            reason_code=reason_code,
        )
        after_invocation = _atomic_observation_outcome(
            invoked_without_request,
            expectation,
        )
        assert isinstance(after_invocation, UnknownOutcome)
        assert after_invocation.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED

        blocked_before_invocation = replace(
            invoked_without_request,
            final_action_invoked=False,
        )
        before_invocation = _atomic_observation_outcome(
            blocked_before_invocation,
            expectation,
        )
        assert before_invocation is not None
        assert before_invocation.kind in {
            AttemptOutcome.NEEDS_REVIEW,
            AttemptOutcome.FAILED_BEFORE_COMMIT,
        }

    malformed = _atomic_observation_outcome(object(), expectation)
    await adapter.cleanup_prepared_action(action=action)

    assert isinstance(malformed, UnknownOutcome)
    assert malformed.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert session.closed is True


@pytest.mark.asyncio
async def test_post_click_identity_redirect_is_unknown(tmp_path) -> None:
    session = _FakeSession(
        post_click_url="https://job-boards.greenhouse.io/fixture/jobs/999999",
    )
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.UNKNOWN
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert session.clicked == 1
    assert session.closed is True


@pytest.mark.parametrize(
    ("post_click", "confirmation_visible"),
    [
        ("generic_thank_you.html", False),
        ("hidden_confirmation.html", False),
        ("verified_confirmation.html", False),
        ("duplicate_confirmation.html", True),
    ],
)
@pytest.mark.asyncio
async def test_generic_hidden_or_nonvisible_confirmation_is_unknown(
    tmp_path,
    post_click,
    confirmation_visible,
) -> None:
    session = _FakeSession(
        post_click=post_click,
        confirmation_visible=confirmation_visible,
    )
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.UNKNOWN
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert session.clicked == 1


@pytest.mark.parametrize(
    "confirmation_reference",
    [
        "0" * 64,
        hashlib.sha256(b"unrelated stable confirmation").hexdigest(),
    ],
)
@pytest.mark.asyncio
async def test_confirmation_reference_must_match_exact_post_action_snapshot(
    tmp_path,
    confirmation_reference,
) -> None:
    session = _FakeSession(
        confirmation_reference_override=confirmation_reference,
    )
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.UNKNOWN
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert session.clicked == 1


@pytest.mark.asyncio
async def test_confirmation_markup_present_before_click_is_unknown(tmp_path) -> None:
    session = _FakeSession(initial="preexisting_confirmation.html")
    adapter, action, permit = await _prepared_live_action(
        tmp_path,
        session,
        inspect_fixture="preexisting_confirmation.html",
    )

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.UNKNOWN
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert session.clicked == 1


@pytest.mark.asyncio
async def test_click_timeout_is_unknown_and_never_retried(tmp_path) -> None:
    session = _FakeSession(click_error=True)
    adapter, action, permit = await _prepared_live_action(tmp_path, session)

    outcome = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert outcome.kind is AttemptOutcome.UNKNOWN
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert replay.kind is AttemptOutcome.FAILED_BEFORE_COMMIT
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.clicked == 1
