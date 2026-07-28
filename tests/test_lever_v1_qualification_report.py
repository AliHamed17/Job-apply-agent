"""Truthfulness and privacy contract for the Lever browser-v1 report."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from submitters.lever_identity import parse_lever_posting_identity
from submitters.lever_v1 import assess_lever_v1_snapshot

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "qualification" / "lever-browser-v1.json"
MARKDOWN_PATH = REPORT_PATH.with_suffix(".md")
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "lever_v1"
IDENTITY = parse_lever_posting_identity(
    "https://jobs.lever.co/sample-company/11111111-2222-4333-8444-555555555555/apply"
)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_CASES = {
    "already_applied.html": ("already_applied", "ALREADY_APPLIED"),
    "application_basic.html": ("form", None),
    "application_consent.html": ("form", None),
    "application_custom_select.html": ("form", None),
    "application_radio_checkbox.html": ("form", None),
    "captcha.html": ("challenge", "CHALLENGE_DETECTED"),
    "closed_job.html": ("closed", "JOB_CLOSED"),
    "disabled_submit.html": ("form", None),
    "duplicate_field.html": ("form", None),
    "generic_success.html": ("selector_drift", "SELECTOR_DRIFT"),
    "hidden_confirmation.html": ("selector_drift", "SELECTOR_DRIFT"),
    "invalid_action.html": ("form", None),
    "job_page.html": ("job", None),
    "mfa.html": ("mfa", "MFA_REQUIRED"),
    "mismatched_confirmation.html": ("selector_drift", "SELECTOR_DRIFT"),
    "multiple_resume.html": ("form", None),
    "outer_aria_disabled.html": ("form", None),
    "outer_content_visibility.html": ("selector_drift", "SELECTOR_DRIFT"),
    "outer_has_proxy_guard.html": ("form", None),
    "outer_inert.html": ("form", None),
    "outer_pointer_events.html": ("form", None),
    "outer_zero_area.html": ("form", None),
    "prompt_injection.html": ("form", None),
    "selector_drift.html": ("selector_drift", "SELECTOR_DRIFT"),
    "session_expired.html": ("login", "SESSION_EXPIRED"),
    "unreviewed_hidden_control.html": ("form", None),
    "verified_confirmation.html": ("confirmation", None),
    "wrong_method.html": ("form", None),
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


def _validate_fixture_only(report: dict[str, object]) -> None:
    assert report["schema_version"] == "ats-browser-qualification-report-v1"
    assert report["adapter"] == {
        "adapter_name": "lever",
        "adapter_version": "1.0.0",
        "execution_contract_version": "two-phase-v2",
        "selector_version": "lever-candidate-v2",
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
    assert safety["real_application_used"] is False


def test_committed_report_cannot_claim_dry_run_canary_scope_or_final_action() -> None:
    report = _load_report()
    _validate_fixture_only(report)

    for mutation in ("tier", "dry_run", "canary", "scope", "final"):
        elevated = deepcopy(report)
        if mutation == "tier":
            elevated["achieved_tier"] = "live_canary_qualified"
        elif mutation == "dry_run":
            elevated["qualification_gates"]["real_url_dry_run"] = "passed"
        elif mutation == "canary":
            elevated["qualification_gates"]["live_canary"] = "passed"
        elif mutation == "scope":
            elevated["qualification_gates"]["qualified_form_scope"] = ["f" * 64]
        else:
            elevated["qualification_gates"]["final_external_action_enabled"] = True
        with pytest.raises(AssertionError):
            _validate_fixture_only(elevated)


def test_report_binds_exact_fixture_bytes_states_and_manifest() -> None:
    evidence = _load_report()["fixture_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["digest_algorithm"] == "sha256-manifest-v1"
    assert evidence["fixture_directory"] == "tests/fixtures/lever_v1"
    assert evidence["fixture_count"] == len(EXPECTED_CASES)
    cases = evidence["cases"]
    assert isinstance(cases, list)
    by_file = {case["file"]: case for case in cases}
    assert set(by_file) == set(EXPECTED_CASES)
    assert {path.name for path in FIXTURE_ROOT.iterdir() if path.is_file()} == set(EXPECTED_CASES)

    manifest = bytearray()
    for filename in sorted(EXPECTED_CASES):
        case = by_file[filename]
        expected_state, expected_reason = EXPECTED_CASES[filename]
        observed_digest = hashlib.sha256((FIXTURE_ROOT / filename).read_bytes()).hexdigest()
        assert case["sha256"] == observed_digest
        assert SHA256.fullmatch(case["sha256"])
        assessment = assess_lever_v1_snapshot(
            (FIXTURE_ROOT / filename).read_text(encoding="utf-8"),
            IDENTITY.apply_url,
            identity=IDENTITY,
        )
        assert assessment.state.value == expected_state
        assert (
            assessment.reason_code.value if assessment.reason_code is not None else None
        ) == expected_reason
        assert case["expected_state"] == expected_state
        assert case["expected_reason_code"] == expected_reason
        manifest.extend(filename.encode("utf-8"))
        manifest.append(0)
        manifest.extend(observed_digest.encode("ascii"))
        manifest.extend(b"\n")

    assert evidence["fixture_digest"] == hashlib.sha256(manifest).hexdigest()
    assert SHA256.fullmatch(evidence["fixture_digest"])


def test_report_pair_contains_no_private_or_live_application_content() -> None:
    report = _load_report()
    assert not (_walk_keys(report) & PROHIBITED_REPORT_KEYS)
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
