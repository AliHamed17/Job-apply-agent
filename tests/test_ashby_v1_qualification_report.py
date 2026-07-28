"""Truthfulness and privacy contract for the Ashby browser v1 report."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from submitters.ashby_v1 import (
    ashby_v1_validation_reason,
    assess_ashby_v1_snapshot,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "qualification" / "ashby-browser-v1.json"
MARKDOWN_PATH = REPORT_PATH.with_suffix(".md")
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "ashby_v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
APPLICATION_URL = (
    "https://jobs.ashbyhq.com/fixture-board/4f44b0a5-5482-4be6-bc11-3d89040b9fa1/application"
)
EXPECTED_CASES = {
    "already_applied.html": ("already_applied", "ALREADY_APPLIED", None),
    "application_base.html": ("form", None, None),
    "application_conditional.html": ("form", None, None),
    "captcha.html": ("challenge", "CHALLENGE_DETECTED", None),
    "closed_job.html": ("closed", "JOB_CLOSED", None),
    "confirmation.html": ("confirmation", None, None),
    "job.html": ("job", None, None),
    "login.html": ("login", "SESSION_EXPIRED", None),
    "mfa.html": ("mfa", "MFA_REQUIRED", None),
    "proxy_actionability.html": ("form", None, None),
    "selector_drift.html": ("selector_drift", "SELECTOR_DRIFT", None),
    "upload_pending.html": ("form", None, "ATTACHMENT_UNVERIFIED"),
    "validation_error.html": ("form", None, "REQUIRED_FIELD_UNKNOWN"),
}
PROHIBITED_REPORT_KEYS = {
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


def _load_report() -> dict[str, object]:
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


def _validate_fixture_only_report(report: dict[str, object]) -> None:
    assert report["schema_version"] == "ats-browser-qualification-report-v1"
    assert report["adapter"] == {
        "adapter_name": "ashby",
        "adapter_version": "1.0.0",
        "execution_contract_version": "two-phase-v2",
        "selector_version": "ashby-candidate-v1",
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
    assert safety["atomic_status_allowlist"] == ("request_or_two_explicit_pre_request_failures")
    assert safety["evaluate_exception_after_gate"] == "unknown_non_retryable"
    assert safety["external_network_used"] is False
    assert safety["final_action_performed"] is False
    assert safety["final_request_resource_type"] == "document"
    assert safety["malformed_release_status"] == "unknown_non_retryable"
    assert safety["post_invocation_without_request"] == "unknown_non_retryable"
    assert safety["private_data_used"] is False
    assert safety["real_application_used"] is False
    assert safety["submitter_proxy_inserted"] is False
    assert safety["transport_contradiction"] == "gate_signal_forces_invoked_unknown"


def test_committed_report_is_fixture_only_and_cannot_enable_live_action() -> None:
    report = _load_report()
    _validate_fixture_only_report(report)

    for mutation in ("tier", "canary", "final_action", "scope"):
        elevated = deepcopy(report)
        if mutation == "tier":
            elevated["achieved_tier"] = "live_canary_qualified"
        elif mutation == "canary":
            elevated["qualification_gates"]["live_canary"] = "passed"
        elif mutation == "final_action":
            elevated["qualification_gates"]["final_external_action_enabled"] = True
        else:
            elevated["qualification_gates"]["qualified_form_scope"] = ["f" * 64]
        with pytest.raises(AssertionError):
            _validate_fixture_only_report(elevated)


def test_report_binds_exact_fixture_bytes_state_and_validation_contract() -> None:
    report = _load_report()
    evidence = report["fixture_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["digest_algorithm"] == "sha256-manifest-v1"
    assert evidence["fixture_directory"] == "tests/fixtures/ashby_v1"
    assert evidence["fixture_count"] == len(EXPECTED_CASES)

    cases = evidence["cases"]
    assert isinstance(cases, list)
    by_file = {case["file"]: case for case in cases}
    assert set(by_file) == set(EXPECTED_CASES)
    assert {path.name for path in FIXTURE_ROOT.iterdir() if path.is_file()} == set(EXPECTED_CASES)

    manifest = bytearray()
    for filename in sorted(EXPECTED_CASES):
        case = by_file[filename]
        expected_state, expected_reason, expected_validation = EXPECTED_CASES[filename]
        assert case["expected_state"] == expected_state
        assert case["expected_reason_code"] == expected_reason
        assert case["expected_validation_reason_code"] == expected_validation

        fixture_path = FIXTURE_ROOT / filename
        fixture_html = fixture_path.read_text(encoding="utf-8")
        observed_digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
        assert SHA256.fullmatch(case["sha256"])
        assert case["sha256"] == observed_digest

        assessment = assess_ashby_v1_snapshot(fixture_html, APPLICATION_URL)
        validation = ashby_v1_validation_reason(fixture_html)
        assert assessment.state.value == expected_state
        assert (
            assessment.reason_code.value if assessment.reason_code is not None else None
        ) == expected_reason
        assert (validation.value if validation is not None else None) == expected_validation

        manifest.extend(filename.encode("utf-8"))
        manifest.append(0)
        manifest.extend(observed_digest.encode("ascii"))
        manifest.extend(b"\n")

    assert SHA256.fullmatch(evidence["fixture_digest"])
    assert evidence["fixture_digest"] == hashlib.sha256(manifest).hexdigest()


def test_report_pair_contains_no_private_or_live_application_content() -> None:
    report = _load_report()
    assert not (_walk_keys(report) & PROHIBITED_REPORT_KEYS)

    json_text = REPORT_PATH.read_text(encoding="utf-8")
    markdown_text = MARKDOWN_PATH.read_text(encoding="utf-8")
    combined = f"{json_text}\n{markdown_text}"
    assert "http://" not in combined.casefold()
    assert "https://" not in combined.casefold()
    assert not re.search(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", combined)
    assert "`fixture_qualified`" in markdown_text
    assert "Real-URL dry run: pending." in markdown_text
    assert "Live canary: pending." in markdown_text
    assert "Final external action: disabled." in markdown_text
