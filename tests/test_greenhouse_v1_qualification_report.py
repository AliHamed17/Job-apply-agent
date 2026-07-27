"""Truthfulness and privacy contract for the Greenhouse browser v1 report."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from submitters.greenhouse_v1 import assess_greenhouse_v1_snapshot

ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "docs" / "qualification" / "greenhouse-browser-v1.json"
MARKDOWN_PATH = REPORT_PATH.with_suffix(".md")
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "greenhouse_v1"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_CASES = {
    "already_applied.html": ("already_applied", "ALREADY_APPLIED"),
    "captcha.html": ("challenge", "CHALLENGE_DETECTED"),
    "closed_job.html": ("closed", "JOB_CLOSED"),
    "compliance_consent.html": ("review", None),
    "conditional_expanded.html": ("review", None),
    "conditional_initial.html": ("review", None),
    "duplicate_confirmation.html": ("selector_drift", "SELECTOR_DRIFT"),
    "embedded_form.html": ("review", None),
    "generic_thank_you.html": ("selector_drift", "SELECTOR_DRIFT"),
    "hidden_confirmation.html": ("selector_drift", "SELECTOR_DRIFT"),
    "hosted_custom_questions.html": ("review", None),
    "job_id_form.html": ("review", None),
    "login.html": ("login", "SESSION_EXPIRED"),
    "non_actionable_submit.html": ("form", None),
    "preexisting_confirmation.html": ("review", None),
    "selector_drift.html": ("selector_drift", "SELECTOR_DRIFT"),
    "submitter_proxy_actionability_drift.html": ("review", None),
    "upload_complete.html": ("review", None),
    "upload_failed.html": ("form", "ATTACHMENT_UNVERIFIED"),
    "upload_pending.html": ("form", "ATTACHMENT_UNVERIFIED"),
    "validation_errors.html": ("form", "REQUIRED_FIELD_UNKNOWN"),
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
    "question_text",
    "resume_content",
    "session_data",
}


def _load_report() -> dict[str, Any]:
    return json.loads(REPORT_PATH.read_text(encoding="utf-8"))


def _canonical_fixture_bytes(path: Path) -> bytes:
    """Return platform-independent UTF-8 bytes for evidence hashing."""

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


def _validate_fixture_only_report(report: dict[str, Any]) -> None:
    assert report["schema_version"] == "ats-browser-qualification-report-v1"
    adapter = report["adapter"]
    assert isinstance(adapter, dict)
    assert adapter == {
        "adapter_name": "greenhouse",
        "adapter_version": "1.0.0",
        "execution_contract_version": "two-phase-v2",
        "selector_version": "greenhouse-candidate-v9",
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
    assert safety["asynchronous_upload_mutations"] == "blocked_pending_exact_qualification"
    assert safety["atomic_outcome_mapping"] == "total_stage_reason_typed_v1"
    assert safety["atomic_status_allowlist"] == "native_or_two_explicit_pre_request_failures"
    assert safety["attachment_transport_scope"] == "local_file_selection_only"
    assert safety["external_network_used"] is False
    assert safety["evaluate_exception_after_gate"] == "unknown_non_retryable"
    assert safety["final_control_actionability"] == "post_proxy_adjacent_exception_safe"
    assert safety["final_action_performed"] is False
    assert safety["final_request_resource_type"] == "document"
    assert safety["intrinsic_submit_exception"] == "invocation_ambiguous_no_cleanup"
    assert safety["post_invocation_without_request"] == "unknown_non_retryable"
    assert safety["private_data_used"] is False
    assert safety["real_application_used"] is False
    assert safety["submitter_proxy_cleanup"] == "all_pre_request_exits_restore_original_form"
    assert safety["transport_contradiction"] == "gate_request_forces_invoked_unknown"


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


def test_report_binds_and_reassesses_the_exact_sanitized_fixture_set() -> None:
    report = _load_report()
    evidence = report["fixture_evidence"]
    assert isinstance(evidence, dict)
    assert evidence["digest_algorithm"] == "sha256-lf-normalized-manifest-v1"
    assert evidence["fixture_directory"] == "tests/fixtures/greenhouse_v1"
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

        assessment = assess_greenhouse_v1_snapshot(fixture_path.read_text(encoding="utf-8"))
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


def test_report_pair_contains_no_private_live_or_network_content() -> None:
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
    assert "Qualified live form scope: empty." in markdown_text
    assert "Final external action: disabled." in markdown_text
