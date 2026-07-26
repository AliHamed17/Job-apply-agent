from __future__ import annotations

import json
import os
import time
from pathlib import Path
from profile.models import UserProfile

import pytest

from core.config import Settings
from core.credentials import CredentialAccessDisabledError, CredentialVault
from core.portal_sessions import (
    PortalSessionError,
    PortalSessionLease,
    portal_session_for_url,
)
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.employer_workflows import (
    EmployerWorkflowConfig,
    load_employer_workflows,
    workflow_for_url,
)
from submitters.form_brain import FieldSpec, FormBrain
from submitters.platforms import detect_platform
from submitters.portal_login import PortalLoginSubmitter
from submitters.workday import (
    WorkdayPageAssessment,
    WorkdaySubmitter,
    assess_workday_page,
)
from worker.submission_attempts import redacted_result_diagnostics

FIXTURES = Path("tests/fixtures/workday")
WORKDAY_URL = (
    "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/Test_JR123456"
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("fixture", "state", "reason"),
    [
        ("job.html", "job", None),
        ("entry_options.html", "entry_options", None),
        ("application_questions.html", "form", None),
        ("review.html", "review", None),
        ("submitted.html", "submitted", "SUBMITTED"),
        ("already_applied.html", "already_applied", "ALREADY_APPLIED"),
        ("session_expired.html", "session_expired", "SESSION_EXPIRED"),
        ("captcha.html", "challenge", "CHALLENGE_DETECTED"),
        ("selector_drift.html", "unknown", "SELECTOR_DRIFT"),
    ],
)
def test_workday_fixture_classification(fixture, state, reason):
    result = assess_workday_page(_fixture(fixture), WORKDAY_URL)
    assert result.state == state
    assert result.terminal_reason == reason


def test_platform_detection_covers_existing_adapters():
    assert detect_platform(WORKDAY_URL) == "workday"
    assert detect_platform("https://boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_platform("https://jobs.lever.co/acme/1") == "lever"
    assert detect_platform("https://jobs.ashbyhq.com/acme/1") == "ashby"
    assert detect_platform("https://careers.example.test/jobs/1") == "generic_portal"
    assert (
        detect_platform("https://careers.example.test/jobs/1?next=linkedin.com/jobs")
        == "generic_portal"
    )


def test_nvidia_workflow_reuses_last_application_and_known_source():
    config = load_employer_workflows("does-not-exist.yaml")
    workflow = workflow_for_url(WORKDAY_URL, config)
    assert workflow.id == "nvidia_workday"
    assert workflow.prefer_last_application is True
    assert workflow.source_path == ["Website", "NVIDIA.COM"]

    generic = workflow_for_url(
        "https://other.wd5.myworkdayjobs.com/job/1",
        EmployerWorkflowConfig(),
    )
    assert generic.id == "generic"
    assert generic.source_path == []


def test_portal_profiles_are_tenant_isolated_and_leased(tmp_path):
    first = portal_session_for_url(WORKDAY_URL, tmp_path)
    second = portal_session_for_url(
        "https://other.wd5.myworkdayjobs.com/job/1",
        tmp_path,
    )
    assert first.profile_dir != second.profile_dir
    assert first.profile_dir.parent == tmp_path.resolve()
    assert first.ready is False

    first.profile_dir.mkdir(parents=True)
    (first.profile_dir / "state").write_text("browser state", encoding="utf-8")
    assert first.ready is False
    first.mark_ready()
    assert first.ready is True

    with PortalSessionLease(first):
        with pytest.raises(PortalSessionError, match="PORTAL_SESSION_BUSY"):
            PortalSessionLease(first).acquire()
    assert not (tmp_path / f".{first.profile_key}.lock").exists()


def test_active_portal_session_owner_is_not_evicted_when_lock_is_old(tmp_path):
    session = portal_session_for_url(WORKDAY_URL, tmp_path)
    lease = PortalSessionLease(session, stale_minutes=1)
    lease.acquire()
    try:
        old = time.time() - 120
        os.utime(lease.lock_path, (old, old))
        with pytest.raises(PortalSessionError, match="PORTAL_SESSION_BUSY"):
            PortalSessionLease(session, stale_minutes=1).acquire()
    finally:
        lease.release()


def test_legacy_credential_vault_fails_closed():
    with pytest.raises(CredentialAccessDisabledError, match="PASSWORD_AUTOFILL_DISABLED"):
        CredentialVault.get_credential_for_url(WORKDAY_URL)


@pytest.mark.asyncio
async def test_unsupported_legacy_portal_never_claims_success():
    result = await PortalLoginSubmitter().submit(
        JobData(
            title="Test",
            apply_url="https://careers.example.taleo.net/job/1",
            source_url="https://careers.example.taleo.net/job/1",
        ),
        GeneratedApplication(),
        {},
    )
    assert result.status == "draft_only"
    assert result.reason_code == "PORTAL_ADAPTER_REQUIRED"
    assert result.confirmation_id is None


class _ScriptedPage:
    def __init__(self, final_state: str = "submitted"):
        self.stage = "job"
        self.final_state = final_state
        self.submit_clicks = 0
        self.url = WORKDAY_URL

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, *_args):
        return None

    async def content(self):
        return "<html><body></body></html>"


class _ScriptedWorkday(WorkdaySubmitter):
    def __init__(self):
        super().__init__()
        self.used_last_seen = None

    async def _assessment(self, page):
        return WorkdayPageAssessment(page.stage)

    async def _click_action(self, page, action):
        if action == "apply" and page.stage == "job":
            page.stage = "entry_options"
            return True
        if action == "use_last_application" and page.stage == "entry_options":
            page.stage = "form"
            return True
        if action == "continue" and page.stage == "form":
            page.stage = "review"
            return True
        if action == "submit" and page.stage == "review":
            page.submit_clicks += 1
            page.stage = page.final_state
            return True
        return False

    async def _action_visible(self, page, action):
        return action == "submit" and page.stage == "review"

    async def _resolve_and_fill_step(self, **kwargs):
        self.used_last_seen = kwargs["used_last_application"]
        return None, ["user_confirmed"], {"radio"}, False


class _IndeterminateClickWorkday(_ScriptedWorkday):
    async def _click_action(self, page, action):
        if action == "submit" and page.stage == "review":
            page.submit_clicks += 1
            raise TimeoutError("navigation interrupted after click")
        return await super()._click_action(page, action)


def _settings(final_submit: bool):
    return Settings(
        _env_file=None,
        draft_only=False,
        dry_run=False,
        portal_final_submit_enabled=final_submit,
        portal_reuse_last_application=True,
    )


def _policy():
    return workflow_for_url(
        WORKDAY_URL,
        load_employer_workflows("does-not-exist.yaml"),
    )


@pytest.mark.asyncio
async def test_workday_reaches_review_without_clicking_final_submit():
    page = _ScriptedPage()
    submitter = _ScriptedWorkday()
    result = await submitter._apply(
        page=page,
        job_url=WORKDAY_URL,
        job=JobData(title="Test", apply_url=WORKDAY_URL),
        application=GeneratedApplication(),
        brain=FormBrain(UserProfile()),
        resume_path=None,
        policy=_policy(),
        settings=_settings(final_submit=False),
    )
    assert result.status == "draft_only"
    assert result.reason_code == "REVIEW_READY"
    assert page.submit_clicks == 0
    assert submitter.used_last_seen is True


@pytest.mark.asyncio
async def test_workday_only_claims_success_after_confirmation():
    page = _ScriptedPage(final_state="submitted")
    result = await _ScriptedWorkday()._apply(
        page=page,
        job_url=WORKDAY_URL,
        job=JobData(title="Test", apply_url=WORKDAY_URL),
        application=GeneratedApplication(),
        brain=FormBrain(UserProfile()),
        resume_path=None,
        policy=_policy(),
        settings=_settings(final_submit=True),
    )
    assert result.status == "submitted"
    assert result.reason_code == "SUBMITTED"
    assert page.submit_clicks == 1


@pytest.mark.asyncio
async def test_workday_unconfirmed_post_click_outcome_is_unknown():
    page = _ScriptedPage(final_state="unknown")
    result = await _ScriptedWorkday()._apply(
        page=page,
        job_url=WORKDAY_URL,
        job=JobData(title="Test", apply_url=WORKDAY_URL),
        application=GeneratedApplication(),
        brain=FormBrain(UserProfile()),
        resume_path=None,
        policy=_policy(),
        settings=_settings(final_submit=True),
    )
    assert result.status == "unknown"
    assert result.reason_code == "SUBMIT_UNCONFIRMED"
    assert result.success is False


@pytest.mark.asyncio
async def test_workday_click_timeout_is_unknown_and_cannot_auto_retry():
    page = _ScriptedPage()
    result = await _IndeterminateClickWorkday()._apply(
        page=page,
        job_url=WORKDAY_URL,
        job=JobData(title="Test", apply_url=WORKDAY_URL),
        application=GeneratedApplication(),
        brain=FormBrain(UserProfile()),
        resume_path=None,
        policy=_policy(),
        settings=_settings(final_submit=True),
    )
    assert page.submit_clicks == 1
    assert result.status == "unknown"
    assert result.reason_code == "SUBMIT_UNCONFIRMED"


def test_generated_answers_cannot_bypass_confirmed_sensitive_evidence():
    field = FieldSpec(
        label="Are you authorized to work in this country?",
        kind="radio",
        options=["Yes", "No"],
        required=True,
    )
    answer = WorkdaySubmitter._generated_answer(
        field,
        {"work_authorization": "Yes"},
    )
    assert answer is None


def test_browser_diagnostics_drop_personal_and_unbounded_values():
    raw = redacted_result_diagnostics(
        "failure person@example.test",
        {
            "selector_version": "workday-candidate-v1",
            "terminal_reason": "REQUIRED_FIELD_UNKNOWN",
            "step_count": 2,
            "events": [
                {
                    "event": "step_resolved",
                    "step": 1,
                    "field_types": ["radio"],
                    "resolver_sources": ["user_confirmed"],
                    "answer": "person@example.test",
                    "url": "https://private.example/job",
                }
            ],
        },
    )
    assert raw is not None
    assert "person@example" not in raw
    assert "private.example" not in raw
    parsed = json.loads(raw)
    assert parsed["terminal_reason"] == "REQUIRED_FIELD_UNKNOWN"
    assert parsed["events"][0]["field_types"] == ["radio"]
