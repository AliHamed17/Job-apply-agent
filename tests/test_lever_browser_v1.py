"""Fail-closed contract tests for LeverBrowserV1."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from core.submission_domain import (
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    ConfirmedSubmittedOutcome,
    FailedBeforeCommitOutcome,
    FinalSubmitPermit,
    NeedsReviewOutcome,
    PreparedFinalActionV1,
    ReasonCode,
    UnknownOutcome,
)
from ingestion.url_utils import normalize_url
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.base import AdapterPreflightContext, TwoPhaseSubmitter
from submitters.lever_identity import LeverPostingIdentity
from submitters.lever_v1 import (
    LeverAdapterBlockedError,
    LeverAttachmentProof,
    LeverBrowserSnapshot,
    LeverBrowserV1,
    LeverFinalActionProof,
)
from submitters.platforms import QualificationTier, adapter_for_platform
from submitters.registry import get_two_phase_registry

FIXTURES = Path(__file__).parent / "fixtures" / "lever_v1"
POSTING = "11111111-2222-4333-8444-555555555555"
JOB_URL = f"https://jobs.lever.co/sample-company/{POSTING}/apply"
NOW = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _FakeSession:
    def __init__(
        self,
        *,
        initial: str = "application_basic.html",
        post_click: str = "verified_confirmation.html",
        attachment_verified: bool = True,
        proof_valid: bool = True,
        click_error: bool = False,
        browser_evidence_visible: bool = True,
        mutate_path: Path | None = None,
        inject_before_action: str | None = None,
    ) -> None:
        self.html = _fixture(initial)
        self.post_click_html = _fixture(post_click)
        self.attachment_verified = attachment_verified
        self.proof_valid = proof_valid
        self.click_error = click_error
        self.browser_evidence_visible = browser_evidence_visible
        self.mutate_path = mutate_path
        self.inject_before_action = inject_before_action
        self.attachment: LeverAttachmentProof | None = None
        self.uploaded_bytes: list[bytes] = []
        self.navigate_calls: list[str] = []
        self.fill_calls = []
        self.prepare_calls = 0
        self.click_calls = 0
        self.close_calls = 0

    async def navigate(self, url: str) -> None:
        self.navigate_calls.append(url)

    async def open_candidate_form(self, identity: LeverPostingIdentity) -> None:
        assert identity.apply_url == JOB_URL
        self.html = _fixture("application_basic.html")

    async def snapshot(self) -> LeverBrowserSnapshot:
        return LeverBrowserSnapshot(html=self.html, url=JOB_URL, locale="en")

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> LeverAttachmentProof:
        assert hashlib.sha256(resume_bytes).hexdigest() == expected_sha256
        self.uploaded_bytes.append(resume_bytes)
        if self.mutate_path is not None:
            self.mutate_path.write_bytes(b"mutated-after-verified-read")
        self.attachment = LeverAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=self.attachment_verified,
            receipt_sha256=("a" * 64 if self.attachment_verified else None),
            resume_control_sha256=("b" * 64 if self.attachment_verified else None),
        )
        return self.attachment

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> LeverAttachmentProof:
        return self.attachment or LeverAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=False,
        )

    async def fill(self, decisions) -> None:
        self.fill_calls.append(decisions)

    async def prepare_final_action(
        self,
        *,
        identity,
        fields,
        decisions,
        form_fingerprint,
        attached_cv_sha256,
    ) -> LeverFinalActionProof:
        self.prepare_calls += 1
        assert len(fields) == len(decisions)
        if self.inject_before_action is not None:
            self.html = _fixture(self.inject_before_action)
        return LeverFinalActionProof(
            identity_sha256=(
                hashlib.sha256(identity.stable_key.encode()).hexdigest()
                if self.proof_valid
                else "0" * 64
            ),
            action_url_sha256=hashlib.sha256(identity.apply_url.encode()).hexdigest(),
            form_fingerprint=form_fingerprint,
            method="POST",
            encoding="multipart/form-data",
            submitter_sha256="c" * 64,
            actionability_sha256="e" * 64,
            resume_control_sha256="b" * 64,
            attached_cv_sha256=attached_cv_sha256,
            payload_commitment_sha256="d" * 64,
            user_field_count=len(fields),
            precommit_mutation_count=0,
        )

    async def click_final_action(self, _proof: LeverFinalActionProof) -> None:
        self.click_calls += 1
        if self.click_error:
            raise TimeoutError("synthetic post-click ambiguity")
        self.html = self.post_click_html

    async def confirmation_reference(
        self,
        identity: LeverPostingIdentity,
    ) -> str | None:
        if not self.browser_evidence_visible:
            return None
        node = BeautifulSoup(self.html, "html.parser").select_one(
            'main[data-qa="application-confirmation"][data-application-id]'
        )
        if (
            node is None
            or node.has_attr("hidden")
            or node.get("data-posting-id") != identity.posting_id
        ):
            return None
        return str(node.get("data-application-id", "")).strip() or None

    async def close(self) -> None:
        self.close_calls += 1


class _Factory:
    def __init__(self, *sessions: _FakeSession) -> None:
        self.sessions = list(sessions)
        self.urls: list[str] = []

    def __call__(self, url: str) -> _FakeSession:
        self.urls.append(url)
        if not self.sessions:
            raise AssertionError("unexpected browser session creation")
        return self.sessions.pop(0)


def _resume(tmp_path: Path) -> tuple[str, str]:
    path = tmp_path / "fixture-cv.pdf"
    path.write_bytes(b"%PDF-1.4\nsanitized fixture resume\n%%EOF\n")
    return str(path.resolve()), hashlib.sha256(path.read_bytes()).hexdigest()


def _profile() -> dict:
    return {
        "personal": {
            "name": "Fixture Candidate",
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
) -> tuple[object, str, str]:
    resume_path, cv_hash = _resume(tmp_path)
    adapter = LeverBrowserV1(
        browser_factory=_Factory(session),
        clock=lambda: NOW,
    )
    plan = await adapter.inspect(
        application_id=7,
        application_revision=3,
        job=_job(),
        application=GeneratedApplication(cv_sha256=cv_hash, profile_version=4),
        user_profile=_profile(),
        resume_path=resume_path,
        selected_cv_id="fixture-cv",
    )
    return plan, resume_path, cv_hash


def _permit(plan) -> FinalSubmitPermit:
    return FinalSubmitPermit(
        attempt_id=19,
        job_url_hash="e" * 64,
        application_revision=plan.application_revision,
        adapter_name=plan.adapter_name,
        adapter_version=plan.adapter_version,
        selector_version=plan.selector_version,
        form_fingerprint=plan.form_fingerprint,
        cv_hash=plan.selected_cv_hash,
        expires_at=NOW + timedelta(minutes=5),
        nonce="one-use-lever-permit",
    )


def _assume_ready(plan):
    """Replace every non-resolved decision with a synthetic resolved one and
    clear plan.blockers, so ready_for_permit is True and preflight's own
    independent re-check (lever_v1.py: `any(decision.disposition is not
    RESOLVED for decision in plan.decisions)`) doesn't short-circuit before
    reaching the actual mechanics these tests exercise.

    application_basic.html's real 3 non-identity-shaped fields (org/"Current
    company", urls_Twitter_, urls_Other_) have no canonical concept in
    core.submission_domain._CANONICAL_LABEL_ALIASES at all, so nothing in
    AnswerPolicyV1 -- not identity resolution, not operator-approved answers,
    not even a configured LLM -- can ever resolve them for real; see
    test_inspection_builds_auditable_plan_and_never_clicks and the P1 plan
    doc for the full trace. Worse: AnswerDecisionV1's own validator forbids a
    RESOLVED decision from carrying a blank value, so there is no way to
    honestly encode "reviewed and left blank" either -- that concept doesn't
    exist in the domain model yet. Both gaps are real and, as of this
    session, unresolved -- but they are orthogonal to what preflight/
    commit-phase tests below actually exercise (attachment binding,
    confirmation-injection resistance, click-timeout handling, proof
    validation): those checks run *after* every field already has a
    resolved answer, and don't care what the answers are. The placeholder
    value below is exactly that -- a schema-valid stand-in for "assume an
    operator already reviewed and answered this through some future
    mechanism" -- not a claim this is what the real field's answer would be.
    """
    resolved = tuple(
        decision
        if decision.disposition is AnswerDisposition.RESOLVED
        else AnswerDecisionV1(
            field_id=decision.field_id,
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.OPERATOR_APPROVED_REUSABLE,
            value="synthetic-test-placeholder",
        )
        for decision in plan.decisions
    )
    return plan.model_copy(update={"blockers": (), "decisions": resolved})


def _context(resume_path: str, cv_hash: str) -> AdapterPreflightContext:
    return AdapterPreflightContext(
        normalized_job_url=normalize_url(JOB_URL),
        selected_cv_id="fixture-cv",
        selected_cv_hash=cv_hash,
        resume_path=resume_path,
    )


def _live_descriptor(fingerprint: str):
    descriptor = adapter_for_platform("lever")
    assert descriptor is not None
    return replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(fingerprint,),
    )


@pytest.mark.asyncio
async def test_inspection_builds_auditable_plan_and_never_clicks(tmp_path) -> None:
    """application_basic.html is the real, 11-field Collate form (see the P1
    plan doc), not the old 3-field candidate-*/FIXTURE_QUALIFIED-era fixture
    this test used to assert against -- ready_for_permit is correctly False
    here, not a regression: 3 of the 11 real fields (org/"Current company",
    urls_Twitter_, urls_Other_) have no canonical concept in
    core.submission_domain._CANONICAL_LABEL_ALIASES at all (not "unmapped
    yet", genuinely no concept -- "current employer"/"twitter" aren't
    identity facts this policy models), so they can never resolve through
    any existing deterministic path (identity, operator-approved, CV
    evidence, or even a configured LLM -- _allowed_llm_evidence_keys also
    requires a recognized canonical/semantic key). That is Lever's fill()
    correctly refusing to guess at fields nobody has ever told it how to
    answer, not a bug -- see the P1 plan doc for the full trace and why this
    is now the real, top blocker for Task 3, independent of everything else
    fixed this session. What IS proven here: 8 of 11 fields are correctly
    auditable, and the 3 identity-shaped ones with data in the fake profile
    (resume, name, email) resolve with real, inspectable provenance -- this
    fixture had no resolvable phone/location/social-link values in the fake
    profile either, so those three abstain for a completely mundane reason
    (sparse test data), not a mapping gap; see
    test_basic_form_observer_is_ordered_bounded_and_attachment_explicit for
    proof each of those *would* resolve given real values."""
    session = _FakeSession()
    plan, _resume_path, cv_hash = await _inspect(tmp_path, session)
    adapter = LeverBrowserV1(browser_factory=_Factory())

    assert isinstance(adapter, TwoPhaseSubmitter)
    assert plan.adapter_name == "lever"
    assert plan.adapter_version == "1.0.0"
    assert plan.selector_version == "lever-candidate-v4"
    assert plan.selected_cv_id == plan.attached_cv_id == "fixture-cv"
    assert plan.selected_cv_hash == plan.attached_cv_hash == cv_hash
    assert [field.field_id for field in plan.fields] == [
        "resume",
        "name",
        "email",
        "phone",
        "location",
        "org",
        "urls_LinkedIn_",
        "urls_Twitter_",
        "urls_GitHub_",
        "urls_Portfolio_",
        "urls_Other_",
    ]
    resolved = {
        decision.field_id: decision.provenance
        for decision in plan.decisions
        if decision.disposition is AnswerDisposition.RESOLVED
    }
    assert resolved == {
        "resume": AnswerProvenance.VERIFIED_ATTACHMENT,
        "name": AnswerProvenance.DETERMINISTIC_IDENTITY,
        "email": AnswerProvenance.DETERMINISTIC_IDENTITY,
    }
    assert plan.ready_for_permit is False
    assert plan.blockers == (ReasonCode.REQUIRED_FIELD_UNKNOWN,)
    assert session.click_calls == 0
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_fixture_qualification_cannot_create_a_final_action(tmp_path) -> None:
    plan, resume_path, cv_hash = await _inspect(tmp_path, _FakeSession())
    forbidden_factory = _Factory()
    adapter = LeverBrowserV1(
        browser_factory=forbidden_factory,
        clock=lambda: NOW,
    )

    outcome = await adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(resume_path, cv_hash),
    )

    assert isinstance(outcome, FailedBeforeCommitOutcome)
    assert outcome.reason_code is ReasonCode.ADAPTER_NOT_QUALIFIED
    assert forbidden_factory.urls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["already_applied.html", "verified_confirmation.html"])
async def test_preexisting_application_state_is_never_a_new_submission(
    tmp_path,
    fixture: str,
) -> None:
    session = _FakeSession(initial=fixture)
    resume_path, cv_hash = _resume(tmp_path)
    adapter = LeverBrowserV1(
        browser_factory=_Factory(session),
        clock=lambda: NOW,
    )

    with pytest.raises(LeverAdapterBlockedError) as exc_info:
        await adapter.inspect(
            application_id=7,
            application_revision=3,
            job=_job(),
            application=GeneratedApplication(cv_sha256=cv_hash, profile_version=4),
            user_profile=_profile(),
            resume_path=resume_path,
            selected_cv_id="fixture-cv",
        )

    assert exc_info.value.reason_code is ReasonCode.ALREADY_APPLIED
    assert session.click_calls == 0
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_live_shaped_preflight_binds_payload_attachment_and_visible_evidence(
    tmp_path,
) -> None:
    plan, resume_path, cv_hash = await _inspect(tmp_path, _FakeSession())
    plan = _assume_ready(plan)
    session = _FakeSession()
    adapter = LeverBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )
    permit = _permit(plan)

    action = await adapter.preflight(
        plan=plan,
        permit=permit,
        context=_context(resume_path, cv_hash),
    )
    assert isinstance(action, PreparedFinalActionV1)
    outcome = await adapter.commit(action=action, permit=permit)
    replay = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert isinstance(outcome, ConfirmedSubmittedOutcome)
    assert outcome.evidence.attempt_id == permit.attempt_id
    assert outcome.evidence.attached_cv_hash == cv_hash
    assert isinstance(replay, FailedBeforeCommitOutcome)
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.prepare_calls == 1
    assert session.click_calls == 1
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_confirmation_injected_before_action_never_becomes_success(
    tmp_path,
) -> None:
    plan, resume_path, cv_hash = await _inspect(tmp_path, _FakeSession())
    plan = _assume_ready(plan)
    session = _FakeSession(inject_before_action="verified_confirmation.html")
    adapter = LeverBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    outcome = await adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(resume_path, cv_hash),
    )

    assert isinstance(outcome, NeedsReviewOutcome)
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert session.click_calls == 0
    await adapter.cleanup_prepared_action(action=None)
    assert session.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_click",
    [
        "generic_success.html",
        "hidden_confirmation.html",
        "mismatched_confirmation.html",
    ],
)
async def test_generic_hidden_or_wrong_posting_confirmation_is_unknown(
    tmp_path,
    post_click: str,
) -> None:
    plan, resume_path, cv_hash = await _inspect(tmp_path, _FakeSession())
    plan = _assume_ready(plan)
    session = _FakeSession(post_click=post_click)
    adapter = LeverBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )
    permit = _permit(plan)
    action = await adapter.preflight(
        plan=plan,
        permit=permit,
        context=_context(resume_path, cv_hash),
    )
    assert isinstance(action, PreparedFinalActionV1)

    outcome = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert session.click_calls == 1


@pytest.mark.asyncio
async def test_post_click_timeout_is_unknown_and_never_retried(tmp_path) -> None:
    plan, resume_path, cv_hash = await _inspect(tmp_path, _FakeSession())
    plan = _assume_ready(plan)
    session = _FakeSession(click_error=True)
    adapter = LeverBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )
    permit = _permit(plan)
    action = await adapter.preflight(
        plan=plan,
        permit=permit,
        context=_context(resume_path, cv_hash),
    )
    assert isinstance(action, PreparedFinalActionV1)

    first = await adapter.commit(action=action, permit=permit)
    second = await adapter.commit(action=action, permit=permit)
    await adapter.cleanup_prepared_action(action=action)

    assert isinstance(first, UnknownOutcome)
    assert first.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert isinstance(second, FailedBeforeCommitOutcome)
    assert second.reason_code is ReasonCode.PERMIT_REPLAYED
    assert session.click_calls == 1


@pytest.mark.asyncio
async def test_invalid_redacted_payload_proof_blocks_before_final_action(tmp_path) -> None:
    plan, resume_path, cv_hash = await _inspect(tmp_path, _FakeSession())
    plan = _assume_ready(plan)
    session = _FakeSession(proof_valid=False)
    adapter = LeverBrowserV1(
        browser_factory=_Factory(session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    result = await adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(resume_path, cv_hash),
    )
    await adapter.cleanup_prepared_action(action=None)

    assert isinstance(result, NeedsReviewOutcome)
    assert result.reason_code is ReasonCode.FORM_CHANGED
    assert session.click_calls == 0
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_required_unknown_question_blocks_review_and_never_fills(tmp_path) -> None:
    session = _FakeSession(initial="application_custom_select.html")
    plan, _resume_path, _cv_hash = await _inspect(tmp_path, session)

    assert plan.ready_for_permit is False
    assert ReasonCode.REQUIRED_FIELD_UNKNOWN in plan.blockers
    assert session.fill_calls == []
    assert session.click_calls == 0


@pytest.mark.asyncio
async def test_selected_cv_bytes_are_read_once_before_path_mutation(tmp_path) -> None:
    resume_path, cv_hash = _resume(tmp_path)
    original = Path(resume_path).read_bytes()
    session = _FakeSession(mutate_path=Path(resume_path))
    adapter = LeverBrowserV1(
        browser_factory=_Factory(session),
        clock=lambda: NOW,
    )

    plan = await adapter.inspect(
        application_id=7,
        application_revision=3,
        job=_job(),
        application=GeneratedApplication(cv_sha256=cv_hash, profile_version=4),
        user_profile=_profile(),
        resume_path=resume_path,
        selected_cv_id="fixture-cv",
    )

    assert plan.attachment_verified is True
    assert session.uploaded_bytes == [original]
    assert hashlib.sha256(Path(resume_path).read_bytes()).hexdigest() != cv_hash
    assert session.click_calls == 0


def test_dry_run_only_lever_is_not_an_ordinary_employer_inspector() -> None:
    registry = get_two_phase_registry()
    descriptor = adapter_for_platform("lever")

    assert registry.get_inspector(_job()) is None
    assert descriptor is not None
    assert descriptor.qualification is QualificationTier.DRY_RUN_ONLY
    assert descriptor.qualified_form_scope == ()
    assert descriptor.allows_final_execution is False
