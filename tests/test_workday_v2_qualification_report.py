"""Truthfulness and privacy contract for the Workday browser v2 report."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path

import pytest

from submitters.workday_v2 import assess_workday_v2_snapshot

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "qualification" / "workday-browser-v2.json"
MARKDOWN_PATH = REPORT_PATH.with_suffix(".md")
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "workday_v2"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_CASES = {
    "already_applied.html": ("already_applied", "ALREADY_APPLIED"),
    "captcha.html": ("challenge", "CHALLENGE_DETECTED"),
    "closed_job.html": ("closed", "JOB_CLOSED"),
    "login.html": ("login", "SESSION_EXPIRED"),
    "mfa.html": ("mfa", "MFA_REQUIRED"),
    "resume_upload.html": ("resume_upload", None),
    "review.html": ("review", None),
    "selector_drift.html": ("selector_drift", "SELECTOR_DRIFT"),
    "verified_confirmation.html": ("confirmation", None),
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


def _canonical_fixture_bytes(path: Path) -> bytes:
    """Return a platform-independent UTF-8 representation for evidence hashing."""

    text = path.read_text(encoding="utf-8")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


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
    adapter = report["adapter"]
    assert isinstance(adapter, dict)
    assert adapter == {
        "adapter_name": "workday",
        "adapter_version": "2.0.3",
        "execution_contract_version": "two-phase-v2",
        "selector_version": "workday-candidate-v2.4",
        "transport": "browser",
    }
    assert report["achieved_tier"] == "fixture_qualified"

    gates = report["qualification_gates"]
    assert isinstance(gates, dict)
    assert gates == {
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
    assert safety["final_request_resource_type"] == "document"
    assert safety["final_control_guards"] == [
        "enabled",
        "aria_enabled",
        "outside_inert_subtree",
        "visible_positive_geometry",
        "pointer_actionable",
    ]
    assert safety["private_data_used"] is False
    assert safety["real_application_used"] is False


def test_committed_report_is_fixture_only_and_cannot_enable_live_action():
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


def test_report_binds_the_exact_sanitized_fixture_set():
    report = _load_report()
    evidence = report["fixture_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["digest_algorithm"] == "sha256-lf-normalized-manifest-v1"
    assert evidence["fixture_directory"] == "tests/fixtures/workday_v2"
    assert evidence["fixture_count"] == len(EXPECTED_CASES)

    cases = evidence["cases"]
    assert isinstance(cases, list)
    by_file = {case["file"]: case for case in cases}
    assert set(by_file) == set(EXPECTED_CASES)
    assert {path.name for path in FIXTURE_ROOT.iterdir() if path.is_file()} == set(EXPECTED_CASES)

    manifest = bytearray()
    for filename in sorted(EXPECTED_CASES):
        case = by_file[filename]
        state, reason_code = EXPECTED_CASES[filename]
        assert case["expected_state"] == state
        assert case["expected_reason_code"] == reason_code
        fixture_path = FIXTURE_ROOT / filename
        observed_digest = hashlib.sha256(_canonical_fixture_bytes(fixture_path)).hexdigest()
        assert SHA256.fullmatch(case["sha256"])
        assert case["sha256"] == observed_digest
        assessment = assess_workday_v2_snapshot(fixture_path.read_text(encoding="utf-8"))
        assert assessment.state.value == state
        assert (
            assessment.reason_code.value if assessment.reason_code is not None else None
        ) == reason_code
        manifest.extend(filename.encode("utf-8"))
        manifest.append(0)
        manifest.extend(observed_digest.encode("ascii"))
        manifest.extend(b"\n")

    assert SHA256.fullmatch(evidence["fixture_digest"])
    assert evidence["fixture_digest"] == hashlib.sha256(manifest).hexdigest()


def test_report_pair_contains_no_private_or_live_application_content():
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
