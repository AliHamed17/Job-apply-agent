"""Login-walled career portals (Workday, Taleo, iCIMS, NVIDIA).

The submitter has two auth paths — saved browser session first, stored
credentials as a fallback. What matters most here is not which path runs, but
that the outcome is reported truthfully.

The previous implementation was not. It filled the form, uploaded the CV,
closed the browser **without ever clicking submit**, and returned
status="submitted" with a fabricated confirmation id. Its except handler
returned status="submitted" too, so a navigation timeout was recorded as a
successful application. These tests pin that shut.
"""

from __future__ import annotations

import inspect

import pytest

from core.config import Settings
from core.credentials import CredentialVault
from jobs.models import JobData
from llm.generation import GeneratedApplication
from submitters.portal_login import PortalLoginSubmitter


def _job(url="https://nvidia.wd5.myworkdayjobs.com/job/1001"):
    return JobData(
        title="Senior AI Engineer",
        company="NVIDIA",
        apply_url=url,
        description="Build LLMs and RAG systems",
    )


def _generated():
    return GeneratedApplication(
        cover_letter="Cover letter text",
        qa_answers={"years_of_experience": "6"},
    )


# ── credential vault (fallback path) ──────────────────────────────────


def test_credential_vault():
    cred_nvidia = CredentialVault.get_credential_for_url(
        "https://nvidia.wd5.myworkdayjobs.com/NVIDIAExternalCareerSite"
    )
    assert "ali.h.10j@gmail.com" in cred_nvidia.username
    assert "nvidia" in cred_nvidia.domain or "workday" in cred_nvidia.domain

    cred_default = CredentialVault.get_credential_for_url("https://unknowncompany.com/apply")
    assert cred_default.username == "ali.h.10j@gmail.com"


# ── routing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://nvidia.wd5.myworkdayjobs.com/job/100",
        "https://acme.taleo.net/careersection/jobdetail.ftl?job=1",
        "https://careers-acme.icims.com/jobs/1234/login",
    ],
)
def test_claims_login_walled_portals(url):
    assert PortalLoginSubmitter().can_submit(_job(url)) is True


def test_declines_when_there_is_no_url():
    assert PortalLoginSubmitter().can_submit(JobData(title="x")) is False


def test_declines_ordinary_ats_urls():
    assert PortalLoginSubmitter().can_submit(_job("https://boards.greenhouse.io/a/jobs/1")) is False


# ── honesty ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_playwright_reports_draft_not_success(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("playwright not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_playwright)

    result = await PortalLoginSubmitter().submit(_job(), _generated(), {})

    assert result.status == "draft_only"
    assert result.confirmation_id is None, "must not invent a confirmation id"


@pytest.mark.asyncio
async def test_missing_apply_url_is_reported(monkeypatch):
    settings = Settings(_env_file=None, portal_browser_profile_dir="")
    monkeypatch.setattr("submitters.portal_login.get_settings", lambda: settings)

    result = await PortalLoginSubmitter().submit(
        JobData(title="x", apply_url="", source_url=""), _generated(), {}
    )
    assert result.status == "draft_only"
    assert result.confirmation_id is None


def test_session_path_is_preferred_over_credentials():
    """PORTAL_BROWSER_PROFILE_DIR must gate which auth path runs."""
    source = inspect.getsource(PortalLoginSubmitter.submit)
    assert "portal_browser_profile_dir" in source
    assert "launch_persistent_context" in source


def test_submit_is_actually_clicked_before_claiming_success():
    """The core regression: the old version never clicked submit.

    Source-level because the alternative is driving a real Workday page. The
    guarantee is structural — status="submitted" must not be reachable
    without both a submit click and a confirmation check preceding it.
    """
    source = inspect.getsource(PortalLoginSubmitter._run)
    assert "_SUBMIT_SELECTORS" in source, "must click a submit control"
    assert "_SUCCESS_MARKERS" in source, "must verify a confirmation appeared"

    submitted_at = source.index('status="submitted"')
    clicked_at = source.index("_SUBMIT_SELECTORS")
    verified_at = source.index("_SUCCESS_MARKERS")
    assert clicked_at < submitted_at, "submit must be clicked before claiming success"
    assert verified_at < submitted_at, "confirmation must be checked before claiming success"


def test_exception_handler_does_not_report_success():
    """The old except handler returned status="submitted" on any error.

    Parsed rather than grepped: a substring search matches the comment that
    documents the old behavior, which is exactly the kind of false positive
    that makes a guard test worthless.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(PortalLoginSubmitter.submit)))
    statuses: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            if getattr(inner.func, "id", None) != "SubmissionResult":
                continue
            for kw in inner.keywords:
                if kw.arg == "status" and isinstance(kw.value, ast.Constant):
                    statuses.append(kw.value.value)

    assert statuses, "expected the handler to build a SubmissionResult"
    assert "submitted" not in statuses, (
        f"an exception handler still reports success: {statuses}"
    )


def test_expired_session_does_not_silently_fall_back_to_password():
    """Retrying with a password after a session expires trips account lockout."""
    source = inspect.getsource(PortalLoginSubmitter._run)
    assert "session expired" in source.lower()
