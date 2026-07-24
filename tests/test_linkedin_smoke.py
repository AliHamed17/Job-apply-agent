from __future__ import annotations

import json
from pathlib import Path
from profile.models import UserProfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.config import Settings
from jobs.models import JobData
from scripts.linkedin_dry_run_smoke import validate_smoke_guard
from submitters.base import BaseSubmitter
from submitters.browser_trace import RedactedTrace
from submitters.form_brain import FormBrain
from submitters.linkedin_v2 import LinkedInV2Submitter


def test_smoke_guard_requires_dry_run_and_operator_authentication() -> None:
    unsafe = Settings(_env_file=None, dry_run=False, secret_key="s" * 32)
    with patch("scripts.linkedin_dry_run_smoke.get_settings", return_value=unsafe):
        with pytest.raises(RuntimeError, match="DRY_RUN"):
            validate_smoke_guard("https://www.linkedin.com/jobs/view/123", "s" * 32)

    safe = Settings(_env_file=None, dry_run=True, secret_key="s" * 32)
    with patch("scripts.linkedin_dry_run_smoke.get_settings", return_value=safe):
        with pytest.raises(RuntimeError, match="authentication"):
            validate_smoke_guard("https://www.linkedin.com/jobs/view/123", "wrong")
        validate_smoke_guard("https://www.linkedin.com/jobs/view/123", "s" * 32)


def test_smoke_guard_rejects_non_linkedin_url() -> None:
    settings = Settings(_env_file=None, dry_run=True, secret_key="s" * 32)
    with patch("scripts.linkedin_dry_run_smoke.get_settings", return_value=settings):
        with pytest.raises(RuntimeError, match="LinkedIn"):
            validate_smoke_guard("https://example.test/jobs/123", "s" * 32)


def test_redacted_trace_never_persists_personal_values(tmp_path: Path) -> None:
    trace = RedactedTrace()
    trace.record(
        "step_resolved",
        step=1,
        field_types=["email", "file"],
        resolver_sources=["user_confirmed"],
        answer="person@example.com",
        url="https://linkedin.com/jobs/view/123",
        cv_text="private resume",
    )
    trace.record("terminal", terminal_reason="DRY_RUN_DISCARDED")
    report = tmp_path / "report.json"
    trace.write_report(report, qualified=True)
    text = report.read_text(encoding="utf-8")
    assert "person@example.com" not in text
    assert "linkedin.com/jobs" not in text
    assert "private resume" not in text
    assert json.loads(text)["qualified"] is True


def test_sanitized_fixtures_cover_expected_variants() -> None:
    root = Path("tests/fixtures/linkedin")
    names = {path.name for path in root.glob("*.html")}
    assert names == {
        "captcha.html",
        "easy_apply_basic.html",
        "missing_confirmation.html",
        "required_field_refusal.html",
        "resume_upload.html",
        "selector_drift.html",
        "session_expired.html",
    }
    detector = SimpleNamespace(detect_captcha=BaseSubmitter.detect_captcha)
    captcha = (root / "captcha.html").read_text(encoding="utf-8")
    assert BaseSubmitter.detect_captcha(detector, captcha)


class _Locator:
    def __init__(self, page, kind: str):
        self.page = page
        self.kind = kind
        self.first = self

    async def is_visible(self, timeout=None):
        return self.kind in {"easy", "submit", "dismiss", "confirm"}

    async def click(self, timeout=None):
        if self.kind == "submit":
            self.page.submit_clicked = True

    async def count(self):
        return 0


class _Page:
    url = "https://www.linkedin.com/jobs/view/123"

    def __init__(self, confirm_discard: bool = True):
        self.submit_clicked = False
        self.confirm_discard = confirm_discard

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_timeout(self, *_args):
        return None

    async def content(self):
        return "<html><body></body></html>"

    def locator(self, selector):
        if "jobs-apply-button" in selector:
            return _Locator(self, "easy")
        if "Submit application" in selector:
            return _Locator(self, "submit")
        if "discard_application_confirm_btn" in selector:
            return _Locator(self, "confirm" if self.confirm_discard else "missing")
        if "Dismiss" in selector:
            return _Locator(self, "dismiss")
        return _Locator(self, "missing")


@pytest.mark.asyncio
async def test_dry_run_never_clicks_submit_and_discards() -> None:
    page = _Page()
    trace = RedactedTrace()
    submitter = LinkedInV2Submitter(trace=trace)
    result = await submitter._apply(
        page,
        page.url,
        JobData(title="Test", apply_url=page.url, source_url=page.url),
        FormBrain(UserProfile()),
        None,
        Settings(_env_file=None, dry_run=True),
        SimpleNamespace(trip_cooldown=lambda: None),
    )
    assert result.error == "DRY_RUN"
    assert not page.submit_clicked
    assert trace.events[-1]["terminal_reason"] == "DRY_RUN_DISCARDED"


@pytest.mark.asyncio
async def test_dry_run_fails_qualification_when_discard_is_unconfirmed() -> None:
    page = _Page(confirm_discard=False)
    trace = RedactedTrace()
    result = await LinkedInV2Submitter(trace=trace)._apply(
        page,
        page.url,
        JobData(title="Test", apply_url=page.url, source_url=page.url),
        FormBrain(UserProfile()),
        None,
        Settings(_env_file=None, dry_run=True),
        SimpleNamespace(trip_cooldown=lambda: None),
    )
    assert not result.success
    assert result.error == "DRY_RUN_DISCARD_FAILED"
    assert not page.submit_clicked
    assert trace.events[-1]["terminal_reason"] == "DISCARD_FAILED"
