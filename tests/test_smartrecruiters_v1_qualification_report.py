"""Truthfulness and privacy contract for the fixture-only qualification."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from submitters.smartrecruiters_identity import (
    parse_smartrecruiters_candidate_identity,
    resolve_smartrecruiters_posting_identity,
)
from submitters.smartrecruiters_v1 import assess_smartrecruiters_v1_snapshot

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "qualification" / "smartrecruiters-browser-v1.json"
MARKDOWN_PATH = REPORT_PATH.with_suffix(".md")
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "smartrecruiters_v1"
JOB_URL = "https://jobs.smartrecruiters.com/FixtureCo/123456789-sanitized-role"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED = {
    "already_applied.html": ("already_applied", "ALREADY_APPLIED"),
    "application_form.html": ("form", None),
    "candidate_job.html": ("job", None),
    "captcha.html": ("challenge", "CHALLENGE_DETECTED"),
    "closed_job.html": ("closed", "JOB_CLOSED"),
    "conditional_drift.html": ("form", None),
    "generic_success.html": ("selector_drift", "SELECTOR_DRIFT"),
    "hidden_confirmation.html": ("selector_drift", "SELECTOR_DRIFT"),
    "invalid_action.html": ("form", None),
    "login.html": ("login", "SESSION_EXPIRED"),
    "mfa.html": ("mfa", "MFA_REQUIRED"),
    "no_privacy_policy.html": ("form", None),
    "outer_has_proxy_guard.html": ("form", None),
    "selector_drift.html": ("selector_drift", "SELECTOR_DRIFT"),
    "verified_confirmation.html": ("confirmation", None),
}
PROHIBITED_KEYS = {
    "answers",
    "candidate_identity",
    "cookies",
    "cv_content",
    "employer_url",
    "field_labels",
    "job_url",
    "page_content",
    "resume_content",
    "session_data",
}


def _load() -> dict[str, object]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key) for key in value),
            *(nested for item in value.values() for nested in _walk_keys(item)),
        }
    if isinstance(value, list):
        return {nested for item in value for nested in _walk_keys(item)}
    return set()


def _validate_fixture_only(report: dict[str, object]) -> None:
    assert report["schema_version"] == "ats-browser-qualification-report-v1"
    assert report["adapter"] == {
        "adapter_name": "smartrecruiters",
        "adapter_version": "1.0.0",
        "execution_contract_version": "two-phase-v2",
        "selector_version": "smartrecruiters-candidate-v1",
        "transport": "browser",
    }
    assert report["achieved_tier"] == "fixture_qualified"
    assert report["qualification_gates"] == {
        "final_external_action_enabled": False,
        "fixture_contract": "passed",
        "live_canary": "pending",
        "qualified_form_scope": [],
        "real_url_dry_run": "pending",
    }
    safety = report["safety_observations"]
    assert isinstance(safety, dict)
    assert safety["external_network_used"] is False
    assert safety["final_action_performed"] is False
    assert safety["private_data_used"] is False
    assert safety["protected_api_enabled"] is False
    assert safety["real_application_used"] is False


def test_report_cannot_self_elevate_fixture_evidence() -> None:
    report = _load()
    _validate_fixture_only(report)
    for mutation in ("tier", "scope", "canary", "send", "api"):
        changed = deepcopy(report)
        if mutation == "tier":
            changed["achieved_tier"] = "live_canary_qualified"
        elif mutation == "scope":
            changed["qualification_gates"]["qualified_form_scope"] = ["f" * 64]
        elif mutation == "canary":
            changed["qualification_gates"]["live_canary"] = "passed"
        elif mutation == "send":
            changed["qualification_gates"]["final_external_action_enabled"] = True
        else:
            changed["safety_observations"]["protected_api_enabled"] = True
        with pytest.raises(AssertionError):
            _validate_fixture_only(changed)


def test_report_binds_exact_sanitized_fixture_manifest() -> None:
    report = _load()
    evidence = report["fixture_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["digest_algorithm"] == "sha256-manifest-v1"
    assert evidence["fixture_directory"] == "tests/fixtures/smartrecruiters_v1"
    assert evidence["fixture_count"] == len(EXPECTED)
    cases = evidence["cases"]
    assert isinstance(cases, list)
    by_file = {case["file"]: case for case in cases}
    assert set(by_file) == set(EXPECTED)
    assert {path.name for path in FIXTURE_ROOT.glob("*.html")} == set(EXPECTED)

    identity_html = (FIXTURE_ROOT / "application_form.html").read_text(encoding="utf-8")
    identity = resolve_smartrecruiters_posting_identity(
        identity_html,
        parse_smartrecruiters_candidate_identity(JOB_URL),
    )
    manifest = bytearray()
    for filename in sorted(EXPECTED):
        case = by_file[filename]
        state, reason = EXPECTED[filename]
        digest = hashlib.sha256((FIXTURE_ROOT / filename).read_bytes()).hexdigest()
        assert SHA256.fullmatch(case["sha256"])
        assert case["sha256"] == digest
        assert case["expected_state"] == state
        assert case["expected_reason_code"] == reason
        assessment = assess_smartrecruiters_v1_snapshot(
            (FIXTURE_ROOT / filename).read_text(encoding="utf-8"),
            JOB_URL,
            identity=identity,
        )
        assert assessment.state.value == state
        assert (
            assessment.reason_code.value if assessment.reason_code is not None else None
        ) == reason
        manifest.extend(filename.encode())
        manifest.append(0)
        manifest.extend(digest.encode())
        manifest.extend(b"\n")
    assert evidence["fixture_digest"] == hashlib.sha256(manifest).hexdigest()


def test_report_pair_contains_no_private_or_live_application_material() -> None:
    report = _load()
    assert not (_walk_keys(report) & PROHIBITED_KEYS)
    combined = (
        REPORT_PATH.read_text(encoding="utf-8") + "\n" + MARKDOWN_PATH.read_text(encoding="utf-8")
    )
    assert "http://" not in combined.casefold()
    assert "https://" not in combined.casefold()
    assert not re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", combined)
    assert "`fixture_qualified`" in combined
    assert "Real-URL dry run: pending." in combined
    assert "Live canary: pending." in combined
    assert "Final external action: disabled." in combined
