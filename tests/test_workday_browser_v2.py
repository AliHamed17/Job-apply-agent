"""Fail-closed contract tests for WorkdayBrowserV2."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from core.async_lifecycle import SameEventLoopLifecycle
from core.form_planning import AnswerPolicyResult, AnswerPolicyV1
from core.submission_domain import (
    AlreadyAppliedOutcome,
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
from submitters.platforms import QualificationTier, adapter_for_platform
from submitters.registry import get_two_phase_registry
from submitters.workday_v2 import (
    WorkdayAdapterBlockedError,
    WorkdayAttachmentProof,
    WorkdayBrowserSnapshot,
    WorkdayBrowserV2,
    WorkdayFinalActionAmbiguousError,
    WorkdayFinalActionReceipt,
    WorkdayFinalCommitExpectation,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workday_v2"
JOB_URL = "https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-1"
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class _FakeSession:
    def __init__(
        self,
        *,
        initial: str = "resume_upload.html",
        post_click_html: str | None = None,
        upload_complete: bool = True,
        click_error: bool = False,
        step_html: tuple[str, ...] | None = None,
        browser_confirmation_visible: bool = True,
        mutate_path_on_upload: Path | None = None,
        pre_click_html: str | None = None,
        pre_click_url: str | None = None,
        invalidate_attachment_before_commit: bool = False,
        post_receipt_snapshot_error: ReasonCode | None = None,
        post_receipt_confirmation_error: ReasonCode | None = None,
        confirmation_reference_override: str = "",
    ) -> None:
        self._steps = list(step_html or (_fixture(initial), _fixture("review.html")))
        self.html = self._steps.pop(0)
        self.post_click_html = post_click_html or _fixture("verified_confirmation.html")
        self.upload_complete = upload_complete
        self.click_error = click_error
        self.browser_confirmation_visible = browser_confirmation_visible
        self.mutate_path_on_upload = mutate_path_on_upload
        self.pre_click_html = pre_click_html
        self.pre_click_url = pre_click_url
        self.invalidate_attachment_before_commit = invalidate_attachment_before_commit
        self.post_receipt_snapshot_error = post_receipt_snapshot_error
        self.post_receipt_confirmation_error = post_receipt_confirmation_error
        self.confirmation_reference_override = confirmation_reference_override
        self.attachment: WorkdayAttachmentProof | None = None
        self.committed = False
        self.uploaded_bytes: list[bytes] = []
        self.navigate_calls: list[str] = []
        self.open_form_calls = 0
        self.fill_calls = []
        self.prepare_calls = 0
        self.click_calls = 0
        self.snapshot_calls = 0
        self.verify_attachment_calls = 0
        self.close_calls = 0
        self.close_loop_ids: list[int] = []
        self.current_url = JOB_URL

    async def navigate(self, url: str) -> None:
        self.navigate_calls.append(url)

    async def open_candidate_form(self) -> None:
        self.open_form_calls += 1

    async def snapshot(self) -> WorkdayBrowserSnapshot:
        self.snapshot_calls += 1
        if self.committed and self.post_receipt_snapshot_error is not None:
            raise WorkdayAdapterBlockedError(self.post_receipt_snapshot_error)
        if self.snapshot_calls == 3:
            if self.pre_click_html is not None:
                self.html = self.pre_click_html
            if self.pre_click_url is not None:
                self.current_url = self.pre_click_url
        return WorkdayBrowserSnapshot(html=self.html, url=self.current_url, locale="en")

    async def ensure_resume_attachment(
        self,
        *,
        resume_bytes: bytes,
        cv_id: str,
        expected_sha256: str,
    ) -> WorkdayAttachmentProof:
        assert hashlib.sha256(resume_bytes).hexdigest() == expected_sha256
        if self.mutate_path_on_upload is not None:
            self.mutate_path_on_upload.write_bytes(b"mutated-after-verified-read")
        self.uploaded_bytes.append(resume_bytes)
        self.attachment = WorkdayAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=self.upload_complete,
            receipt_sha256=("1" * 64 if self.upload_complete else None),
        )
        return self.attachment

    async def verify_resume_attachment(
        self,
        *,
        cv_id: str,
        expected_sha256: str,
    ) -> WorkdayAttachmentProof:
        self.verify_attachment_calls += 1
        if self.invalidate_attachment_before_commit and self.verify_attachment_calls >= 2:
            return WorkdayAttachmentProof(
                cv_id=cv_id,
                cv_sha256=expected_sha256,
                upload_complete=False,
            )
        return self.attachment or WorkdayAttachmentProof(
            cv_id=cv_id,
            cv_sha256=expected_sha256,
            upload_complete=False,
        )

    async def fill(self, decisions) -> None:
        self.fill_calls.append(decisions)

    async def advance_reversible_step(self) -> None:
        self.prepare_calls += 1
        if not self._steps:
            raise AssertionError("unexpected reversible step advance")
        self.html = self._steps.pop(0)

    async def commit_final_action(
        self,
        expectation: WorkdayFinalCommitExpectation,
    ) -> WorkdayFinalActionReceipt:
        self.click_calls += 1
        if self.click_error:
            raise WorkdayFinalActionAmbiguousError("synthetic post-click ambiguity")
        self.html = self.post_click_html
        self.committed = True
        payload_sha256 = "c" * 64
        request_digest = hashlib.sha256(
            (
                f"workday-final-request-v1|{expectation.request_contract.digest}|{payload_sha256}"
            ).encode()
        ).hexdigest()
        return WorkdayFinalActionReceipt(
            target_digest=expectation.request_contract.digest,
            payload_sha256=payload_sha256,
            request_digest=request_digest,
        )

    async def confirmation_reference(self) -> str | None:
        if self.post_receipt_confirmation_error is not None:
            raise WorkdayAdapterBlockedError(self.post_receipt_confirmation_error)
        if self.confirmation_reference_override:
            return self.confirmation_reference_override
        if not self.browser_confirmation_visible:
            return None
        node = BeautifulSoup(self.html, "html.parser").select_one(
            'main[data-automation-id="confirmationPage"][data-application-id]'
        )
        if node is None or node.has_attr("hidden"):
            return None
        return str(node.get("data-application-id", "")).strip() or None

    async def close(self) -> None:
        self.close_calls += 1
        self.close_loop_ids.append(id(asyncio.get_running_loop()))


class _SessionFactory:
    def __init__(self, *sessions: _FakeSession) -> None:
        self.sessions = list(sessions)
        self.urls: list[str] = []

    def __call__(self, url: str) -> _FakeSession:
        self.urls.append(url)
        if not self.sessions:
            raise AssertionError("unexpected browser session creation")
        return self.sessions.pop(0)


class _BlockFirstFieldOnce(AnswerPolicyV1):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def plan_fields(self, fields, context):
        self.calls += 1
        if self.calls == 1:
            return AnswerPolicyResult(
                decisions=tuple(
                    AnswerDecisionV1(
                        field_id=field.field_id,
                        disposition=AnswerDisposition.OPERATOR_REQUIRED,
                        provenance=AnswerProvenance.ABSTAINED,
                        reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
                    )
                    for field in fields
                ),
                blockers=(ReasonCode.REQUIRED_FIELD_UNKNOWN,),
            )
        return await super().plan_fields(fields, context)


def _resume(tmp_path: Path) -> tuple[str, str]:
    path = tmp_path / "fixture-cv.pdf"
    path.write_bytes(b"%PDF-1.4\nsanitized fixture resume\n%%EOF\n")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return str(path.resolve()), digest


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


def _identity_step(*, include_email: bool = False, drift_label: bool = False) -> str:
    email = (
        """
        <div data-automation-id="formField" data-field-id="contact_email"
             data-canonical-name="email" aria-required="true">
          <label for="contact-email">Email address</label>
          <input id="contact-email" type="email" maxlength="200" required>
        </div>
        """
        if include_email
        else ""
    )
    return f"""
    <main>
      <div data-automation-id="formField" data-field-id="first_name"
           data-canonical-name="first_name" aria-required="true">
        <label for="first-name">{"Legal first name" if drift_label else "First name"}</label>
        <input id="first-name" type="text" maxlength="100" required>
      </div>
      {email}
      <button data-automation-id="bottom-navigation-next-button">Next</button>
    </main>
    """


def _resume_only_step() -> str:
    return """
    <main>
      <section data-automation-id="resumeUpload">
        <div data-automation-id="formField" data-field-id="resume"
             data-canonical-name="resume" aria-required="true">
          <label for="resume-file">Resume</label>
          <input id="resume-file" data-automation-id="file-upload-input"
                 type="file" accept=".pdf,application/pdf" required>
        </div>
      </section>
      <button data-automation-id="bottom-navigation-next-button">Next</button>
    </main>
    """


def _email_step() -> str:
    return """
    <main>
      <div data-automation-id="formField" data-field-id="contact_email"
           data-canonical-name="email" aria-required="true">
        <label for="contact-email">Email address</label>
        <input id="contact-email" type="email" maxlength="200" required>
      </div>
      <button data-automation-id="bottom-navigation-next-button">Next</button>
    </main>
    """


def _review_with_field_drift() -> str:
    return f"""
    <form data-automation-id="reviewPage" action="{JOB_URL}/apply" method="post"
          enctype="multipart/form-data">
      <div data-automation-id="formField" data-field-id="first_name"
           data-canonical-name="first_name" aria-required="true">
        <label for="changed-first-name">Changed first name</label>
        <input id="changed-first-name" type="text" maxlength="40" required>
      </div>
      <button data-automation-id="submitApplication" type="submit">Submit</button>
    </form>
    """


def _review_with_action(action: str) -> str:
    return f"""
    <form data-automation-id="reviewPage" action="{action}" method="post"
          enctype="multipart/form-data">
      <button data-automation-id="submitApplication" type="submit">Submit</button>
    </form>
    """


async def _inspect(
    tmp_path: Path,
    session: _FakeSession,
) -> tuple[WorkdayBrowserV2, object, str, str]:
    resume_path, cv_hash = _resume(tmp_path)
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(session),
        clock=lambda: NOW,
    )
    plan = await adapter.inspect(
        application_id=7,
        application_revision=3,
        job=_job(),
        application=GeneratedApplication(
            cv_sha256=cv_hash,
            profile_version=4,
        ),
        user_profile=_profile(),
        resume_path=resume_path,
        selected_cv_id="fixture-cv",
    )
    return adapter, plan, resume_path, cv_hash


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


def _context(resume_path: str, cv_hash: str) -> AdapterPreflightContext:
    return AdapterPreflightContext(
        normalized_job_url=normalize_url(JOB_URL),
        selected_cv_id="fixture-cv",
        selected_cv_hash=cv_hash,
        resume_path=resume_path,
    )


def _live_descriptor(fingerprint: str):
    descriptor = adapter_for_platform("workday")
    assert descriptor is not None
    return replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(fingerprint,),
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://status.workday.com/job/fake",
        "https://api.wd5.myworkdayjobs.com/job/fake",
        "https://www.myworkday.com/job/fake",
    ],
)
def test_non_candidate_workday_hosts_cannot_enter_adapter(url: str) -> None:
    factory = _SessionFactory()
    adapter = WorkdayBrowserV2(
        browser_factory=factory,
        clock=lambda: NOW,
    )

    assert (
        adapter.can_inspect(JobData(title="Spoofed role", company="Spoofed", apply_url=url))
        is False
    )
    assert factory.urls == []


@pytest.mark.asyncio
async def test_inspect_builds_ready_plan_and_always_closes_its_session(tmp_path) -> None:
    inspect_session = _FakeSession()

    adapter, plan, _resume_path, cv_hash = await _inspect(tmp_path, inspect_session)

    assert isinstance(adapter, TwoPhaseSubmitter)
    assert plan.ready_for_permit is True
    assert plan.selected_cv_id == plan.attached_cv_id == "fixture-cv"
    assert plan.selected_cv_hash == plan.attached_cv_hash == cv_hash
    assert [field.field_id for field in plan.fields] == [
        "resume",
        "first_name",
        "contact_email",
    ]
    assert inspect_session.close_calls == 1
    assert inspect_session.click_calls == 0


@pytest.mark.asyncio
async def test_inspection_fingerprints_every_reversible_step(tmp_path) -> None:
    session = _FakeSession(
        step_html=(
            _resume_only_step(),
            _identity_step(include_email=True),
            _fixture("review.html"),
        )
    )

    _adapter, plan, _resume_path, _cv_hash = await _inspect(tmp_path, session)

    assert [field.field_id for field in plan.fields] == [
        "resume",
        "first_name",
        "contact_email",
    ]
    assert [field.position for field in plan.fields] == [0, 1, 2]
    assert session.prepare_calls == 2
    assert len(session.fill_calls) == 2
    assert session.click_calls == 0
    assert session.close_calls == 1


@pytest.mark.asyncio
async def test_preflight_never_advances_through_an_unplanned_later_step(tmp_path) -> None:
    inspect_session = _FakeSession(
        step_html=(
            _resume_only_step(),
            _identity_step(include_email=True),
            _fixture("review.html"),
        )
    )
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    changed_second_step = _identity_step(include_email=True, drift_label=True)
    preflight_session = _FakeSession(
        step_html=(
            _resume_only_step(),
            changed_second_step,
            _fixture("review.html"),
        )
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    result = await adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(resume_path, cv_hash),
    )

    assert isinstance(result, NeedsReviewOutcome)
    assert result.reason_code is ReasonCode.FORM_CHANGED
    assert preflight_session.prepare_calls == 1
    assert len(preflight_session.fill_calls) == 1
    assert preflight_session.click_calls == 0
    await adapter.cleanup_prepared_action(action=None)


@pytest.mark.asyncio
async def test_review_to_preflight_action_drift_cannot_prepare_a_click(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(
        step_html=(
            _fixture("resume_upload.html"),
            _review_with_action(f"{JOB_URL}/apply?source=changed"),
        )
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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
    assert preflight_session.click_calls == 0
    await adapter.cleanup_prepared_action(action=None)


@pytest.mark.asyncio
async def test_blocked_partial_plan_never_advances_and_reinspection_must_finish(
    tmp_path,
) -> None:
    first_session = _FakeSession(
        step_html=(
            _identity_step(),
            _resume_only_step(),
            _fixture("review.html"),
        )
    )
    second_session = _FakeSession(
        step_html=(
            _identity_step(),
            _resume_only_step(),
            _fixture("review.html"),
        )
    )
    planner = _BlockFirstFieldOnce()
    resume_path, cv_hash = _resume(tmp_path)
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(first_session, second_session),
        clock=lambda: NOW,
    )
    generated = GeneratedApplication(cv_sha256=cv_hash, profile_version=4)

    partial = await adapter.inspect(
        application_id=7,
        application_revision=3,
        job=_job(),
        application=generated,
        user_profile=_profile(),
        resume_path=resume_path,
        selected_cv_id="fixture-cv",
        answer_policy=planner,
    )

    assert [field.field_id for field in partial.fields] == ["first_name"]
    assert partial.attachment_verified is False
    assert partial.ready_for_permit is False
    assert partial.blockers == (
        ReasonCode.REQUIRED_FIELD_UNKNOWN,
        ReasonCode.ATTACHMENT_UNVERIFIED,
        ReasonCode.FORM_PLAN_INCOMPLETE,
    )
    assert first_session.prepare_calls == 0
    assert first_session.fill_calls == []
    assert first_session.click_calls == 0
    assert first_session.close_calls == 1

    complete = await adapter.inspect(
        application_id=7,
        application_revision=4,
        job=_job(),
        application=generated,
        user_profile=_profile(),
        resume_path=resume_path,
        selected_cv_id="fixture-cv",
        answer_policy=planner,
    )

    assert [field.field_id for field in complete.fields] == ["first_name", "resume"]
    assert complete.attachment_verified is True
    assert complete.blockers == ()
    assert complete.ready_for_permit is True
    assert second_session.prepare_calls == 2
    assert second_session.click_calls == 0
    assert second_session.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_name", ["already_applied.html", "verified_confirmation.html"])
async def test_preexisting_application_state_never_becomes_new_submission(
    tmp_path,
    fixture_name,
) -> None:
    session = _FakeSession(initial=fixture_name)
    resume_path, cv_hash = _resume(tmp_path)
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(session),
        clock=lambda: NOW,
    )

    with pytest.raises(WorkdayAdapterBlockedError) as exc_info:
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
async def test_fixture_qualified_descriptor_makes_final_execution_impossible(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    forbidden_factory = _SessionFactory()
    fixture_adapter = WorkdayBrowserV2(
        browser_factory=forbidden_factory,
        clock=lambda: NOW,
    )

    result = await fixture_adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(resume_path, cv_hash),
    )

    assert isinstance(result, FailedBeforeCommitOutcome)
    assert result.reason_code is ReasonCode.ADAPTER_NOT_QUALIFIED
    assert forbidden_factory.urls == []


@pytest.mark.asyncio
async def test_missing_or_changed_attachment_blocks_before_prepared_action(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(upload_complete=False)
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    result = await adapter.preflight(
        plan=plan,
        permit=_permit(plan),
        context=_context(resume_path, cv_hash),
    )

    assert isinstance(result, NeedsReviewOutcome)
    assert result.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED
    assert preflight_session.prepare_calls == 0
    assert preflight_session.click_calls == 0
    await adapter.cleanup_prepared_action(action=None)
    assert preflight_session.close_calls == 1


@pytest.mark.asyncio
async def test_inspection_uploads_verified_immutable_bytes_if_path_mutates(
    tmp_path,
) -> None:
    resume_path, cv_hash = _resume(tmp_path)
    original = Path(resume_path).read_bytes()
    session = _FakeSession(mutate_path_on_upload=Path(resume_path))
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(session),
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
    assert hashlib.sha256(session.uploaded_bytes[0]).hexdigest() == cv_hash
    assert hashlib.sha256(Path(resume_path).read_bytes()).hexdigest() != cv_hash
    assert session.click_calls == 0


def test_fresh_preflight_session_definitive_outcome_cleanup_uses_lifecycle(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = asyncio.run(_inspect(tmp_path, inspect_session))
    preflight_session = _FakeSession(initial="already_applied.html")
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )

    with SameEventLoopLifecycle() as lifecycle:
        result = lifecycle.run(
            adapter.preflight(
                plan=plan,
                permit=_permit(plan),
                context=_context(resume_path, cv_hash),
            )
        )
        cleanup_loop_id = lifecycle.run(_running_loop_id())
        lifecycle.run(adapter.cleanup_prepared_action(action=None))

    assert isinstance(result, AlreadyAppliedOutcome)
    assert preflight_session.click_calls == 0
    assert preflight_session.close_calls == 1
    assert preflight_session.close_loop_ids == [cleanup_loop_id]


async def _running_loop_id() -> int:
    return id(asyncio.get_running_loop())


def test_successful_preflight_clicks_once_requires_bound_evidence_and_closes(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = asyncio.run(_inspect(tmp_path, inspect_session))
    preflight_session = _FakeSession()
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
        descriptor=_live_descriptor(plan.form_fingerprint),
        clock=lambda: NOW,
    )
    permit = _permit(plan)

    with SameEventLoopLifecycle() as lifecycle:
        action = lifecycle.run(
            adapter.preflight(
                plan=plan,
                permit=permit,
                context=_context(resume_path, cv_hash),
            )
        )
        assert isinstance(action, PreparedFinalActionV1)
        outcome = lifecycle.run(adapter.commit(action=action, permit=permit))
        replay = lifecycle.run(adapter.commit(action=action, permit=permit))
        cleanup_loop_id = lifecycle.run(_running_loop_id())
        lifecycle.run(adapter.cleanup_prepared_action(action=action))

    assert isinstance(outcome, ConfirmedSubmittedOutcome)
    assert outcome.evidence.attempt_id == permit.attempt_id
    assert outcome.evidence.form_fingerprint == plan.form_fingerprint
    assert outcome.evidence.attached_cv_hash == cv_hash
    assert isinstance(replay, FailedBeforeCommitOutcome)
    assert replay.reason_code is ReasonCode.PERMIT_REPLAYED
    assert preflight_session.click_calls == 1
    assert preflight_session.close_calls == 1
    assert preflight_session.close_loop_ids == [cleanup_loop_id]
    assert inspect_session.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_click_html",
    [
        "<main><h1>Application submitted successfully</h1></main>",
        '<main data-automation-id="confirmationPage"><h1>Submitted</h1></main>',
        (
            '<main data-automation-id="confirmationPage" data-application-id="  ">'
            "<h1>Loading</h1></main>"
        ),
        (
            '<main data-automation-id="confirmationPage" data-application-id="old" '
            "hidden><h1>Submitted</h1></main>"
        ),
    ],
)
async def test_generic_hidden_or_incomplete_confirmation_is_unknown(
    tmp_path,
    post_click_html,
) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(post_click_html=post_click_html)
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert preflight_session.click_calls == 1
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
async def test_static_confirmation_markup_without_browser_visibility_is_unknown(
    tmp_path,
) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(browser_confirmation_visible=False)
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point", ["snapshot", "reference"])
async def test_every_post_receipt_guard_error_is_unknown(
    tmp_path,
    failure_point,
) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(
        post_receipt_snapshot_error=(
            ReasonCode.FORM_CHANGED if failure_point == "snapshot" else None
        ),
        post_receipt_confirmation_error=(
            ReasonCode.SESSION_EXPIRED if failure_point == "reference" else None
        ),
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert preflight_session.click_calls == 1
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
async def test_browser_reference_must_match_exact_post_action_snapshot(
    tmp_path,
) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(
        confirmation_reference_override="DIFFERENT-SANITIZED-REFERENCE",
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert preflight_session.click_calls == 1
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
async def test_post_receipt_evidence_verifier_error_is_unknown(
    tmp_path,
    monkeypatch,
) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession()
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    def _raise_after_receipt(*_args, **_kwargs):
        raise RuntimeError("synthetic evidence verifier failure")

    monkeypatch.setattr(
        "submitters.workday_v2.verify_submission_evidence",
        _raise_after_receipt,
    )

    outcome = await adapter.commit(action=action, permit=permit)

    assert isinstance(outcome, UnknownOutcome)
    assert outcome.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert preflight_session.click_calls == 1
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
async def test_commit_rejects_confirmation_inserted_after_preflight_before_click(
    tmp_path,
) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(
        pre_click_html=_fixture("verified_confirmation.html"),
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert not isinstance(outcome, ConfirmedSubmittedOutcome)
    assert preflight_session.click_calls == 0
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
async def test_commit_rejects_form_fingerprint_drift_before_click(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(
        pre_click_html=_review_with_field_drift(),
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(outcome, NeedsReviewOutcome)
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert preflight_session.click_calls == 0
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
async def test_commit_rejects_final_action_target_drift_before_click(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(
        pre_click_html=_review_with_action(f"{JOB_URL}/apply"),
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(outcome, NeedsReviewOutcome)
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert preflight_session.click_calls == 0
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
async def test_commit_rejects_attachment_invalidation_before_click(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(
        invalidate_attachment_before_commit=True,
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(outcome, NeedsReviewOutcome)
    assert outcome.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED
    assert preflight_session.click_calls == 0
    await adapter.cleanup_prepared_action(action=action)


@pytest.mark.asyncio
async def test_commit_rejects_same_tenant_other_job_redirect_before_click(
    tmp_path,
) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(
        pre_click_url="https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-999",
    )
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(outcome, NeedsReviewOutcome)
    assert outcome.reason_code is ReasonCode.FORM_CHANGED
    assert preflight_session.click_calls == 0
    await adapter.cleanup_prepared_action(action=action)


def test_attachment_proof_requires_fresh_receipt_binding() -> None:
    stale = WorkdayAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=True,
    )
    fresh = WorkdayAttachmentProof(
        cv_id="fixture-cv",
        cv_sha256="a" * 64,
        upload_complete=True,
        receipt_sha256="b" * 64,
    )

    assert stale.matches(cv_id="fixture-cv", cv_sha256="a" * 64) is False
    assert fresh.matches(cv_id="fixture-cv", cv_sha256="a" * 64) is True


@pytest.mark.asyncio
async def test_click_timeout_is_unknown_and_never_retried(tmp_path) -> None:
    inspect_session = _FakeSession()
    _adapter, plan, resume_path, cv_hash = await _inspect(tmp_path, inspect_session)
    preflight_session = _FakeSession(click_error=True)
    adapter = WorkdayBrowserV2(
        browser_factory=_SessionFactory(preflight_session),
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

    assert isinstance(first, UnknownOutcome)
    assert first.reason_code is ReasonCode.FINAL_ACTION_UNCONFIRMED
    assert isinstance(second, FailedBeforeCommitOutcome)
    assert second.reason_code is ReasonCode.PERMIT_REPLAYED
    assert preflight_session.click_calls == 1
    await adapter.cleanup_prepared_action(action=action)


def test_fixture_qualified_workday_is_not_an_ordinary_employer_inspector() -> None:
    first = get_two_phase_registry()
    second = get_two_phase_registry()

    assert first is second
    assert first.get_inspector(_job()) is None
    descriptor = adapter_for_platform("workday")
    assert descriptor is not None
    assert descriptor.qualification is QualificationTier.FIXTURE_QUALIFIED
    assert descriptor.qualified_form_scope == ()
    assert descriptor.allows_final_execution is False
