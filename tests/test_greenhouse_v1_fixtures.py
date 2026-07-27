"""Sanitized offline selector contract for Greenhouse browser v1."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from core.submission_domain import FieldType, ReasonCode, SensitiveCategory
from submitters.greenhouse_v1 import (
    GreenhousePageState,
    GreenhouseVariant,
    assess_greenhouse_v1_snapshot,
    detect_greenhouse_variant,
    greenhouse_v1_form_fingerprint,
    observe_greenhouse_v1_fields,
)

FIXTURES = Path(__file__).parent / "fixtures" / "greenhouse_v1"
FORM_ACTION_BINDING = "e" * 64


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("fixture_name", "state", "reason"),
    [
        ("hosted_custom_questions.html", GreenhousePageState.REVIEW, None),
        ("embedded_form.html", GreenhousePageState.REVIEW, None),
        ("job_id_form.html", GreenhousePageState.REVIEW, None),
        ("compliance_consent.html", GreenhousePageState.REVIEW, None),
        ("conditional_initial.html", GreenhousePageState.REVIEW, None),
        ("conditional_expanded.html", GreenhousePageState.REVIEW, None),
        (
            "captcha.html",
            GreenhousePageState.CHALLENGE,
            ReasonCode.CHALLENGE_DETECTED,
        ),
        ("closed_job.html", GreenhousePageState.CLOSED, ReasonCode.JOB_CLOSED),
        (
            "already_applied.html",
            GreenhousePageState.ALREADY_APPLIED,
            ReasonCode.ALREADY_APPLIED,
        ),
        ("login.html", GreenhousePageState.LOGIN, ReasonCode.SESSION_EXPIRED),
        (
            "selector_drift.html",
            GreenhousePageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        ),
        ("verified_confirmation.html", GreenhousePageState.CONFIRMATION, None),
        (
            "generic_thank_you.html",
            GreenhousePageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        ),
        (
            "hidden_confirmation.html",
            GreenhousePageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        ),
        (
            "duplicate_confirmation.html",
            GreenhousePageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        ),
        (
            "upload_pending.html",
            GreenhousePageState.FORM,
            ReasonCode.ATTACHMENT_UNVERIFIED,
        ),
        (
            "upload_failed.html",
            GreenhousePageState.FORM,
            ReasonCode.ATTACHMENT_UNVERIFIED,
        ),
        ("upload_complete.html", GreenhousePageState.REVIEW, None),
        ("non_actionable_submit.html", GreenhousePageState.FORM, None),
        (
            "submitter_proxy_actionability_drift.html",
            GreenhousePageState.REVIEW,
            None,
        ),
        (
            "validation_errors.html",
            GreenhousePageState.FORM,
            ReasonCode.REQUIRED_FIELD_UNKNOWN,
        ),
        ("preexisting_confirmation.html", GreenhousePageState.REVIEW, None),
    ],
)
def test_fixture_state_contract(
    fixture_name: str,
    state: GreenhousePageState,
    reason: ReasonCode | None,
) -> None:
    assessment = assess_greenhouse_v1_snapshot(_fixture(fixture_name))

    assert assessment.state is state
    assert assessment.reason_code is reason


@pytest.mark.parametrize(
    ("fixture_name", "variant"),
    [
        ("hosted_custom_questions.html", GreenhouseVariant.HOSTED),
        ("embedded_form.html", GreenhouseVariant.EMBEDDED),
        ("job_id_form.html", GreenhouseVariant.JOB_ID),
    ],
)
def test_exact_form_variant_is_part_of_the_contract(
    fixture_name: str,
    variant: GreenhouseVariant,
) -> None:
    html = _fixture(fixture_name)
    fields = observe_greenhouse_v1_fields(html)

    assert detect_greenhouse_variant(html) is variant
    assert greenhouse_v1_form_fingerprint(
        fields,
        variant,
        FORM_ACTION_BINDING,
    ) != greenhouse_v1_form_fingerprint(
        fields,
        next(candidate for candidate in GreenhouseVariant if candidate is not variant),
        FORM_ACTION_BINDING,
    )


def test_hosted_custom_controls_capture_exact_options_and_constraints() -> None:
    fields = observe_greenhouse_v1_fields(_fixture("hosted_custom_questions.html"))
    by_id = {field.field_id: field for field in fields}

    assert tuple(by_id) == (
        "first_name",
        "email",
        "resume",
        "question_python",
        "question_work_mode",
    )
    assert by_id["first_name"].field_type is FieldType.TEXT
    assert by_id["email"].field_type is FieldType.EMAIL
    assert by_id["resume"].field_type is FieldType.FILE
    assert by_id["resume"].constraints.accepted_file_types == (".pdf", ".docx")
    assert by_id["question_python"].constraints.min_value == 0
    assert by_id["question_python"].constraints.max_value == 60
    assert tuple(
        (option.option_id, option.value, option.label)
        for option in by_id["question_work_mode"].options
    ) == (
        ("remote", "remote", "Remote"),
        ("hybrid", "hybrid", "Hybrid"),
    )


def test_compliance_and_consent_are_explicitly_sensitive() -> None:
    fields = observe_greenhouse_v1_fields(_fixture("compliance_consent.html"))
    by_id = {field.field_id: field for field in fields}

    assert by_id["gender"].field_type is FieldType.RADIO
    assert by_id["gender"].sensitive_category is SensitiveCategory.DEMOGRAPHIC
    assert by_id["privacy_consent"].field_type is FieldType.CONSENT
    assert by_id["privacy_consent"].sensitive_category is SensitiveCategory.CONSENT
    assert by_id["truth_attestation"].field_type is FieldType.ATTESTATION
    assert by_id["truth_attestation"].sensitive_category is SensitiveCategory.ATTESTATION


def test_conditional_field_drift_changes_the_fingerprint() -> None:
    initial = observe_greenhouse_v1_fields(_fixture("conditional_initial.html"))
    expanded = observe_greenhouse_v1_fields(_fixture("conditional_expanded.html"))

    assert len(expanded) == len(initial) + 1
    assert greenhouse_v1_form_fingerprint(
        initial,
        GreenhouseVariant.HOSTED,
        FORM_ACTION_BINDING,
    ) != greenhouse_v1_form_fingerprint(
        expanded,
        GreenhouseVariant.HOSTED,
        FORM_ACTION_BINDING,
    )


def test_form_action_binding_is_part_of_the_reviewed_fingerprint() -> None:
    fields = observe_greenhouse_v1_fields(_fixture("embedded_form.html"))

    assert greenhouse_v1_form_fingerprint(
        fields,
        GreenhouseVariant.EMBEDDED,
        "e" * 64,
    ) != greenhouse_v1_form_fingerprint(
        fields,
        GreenhouseVariant.EMBEDDED,
        "f" * 64,
    )


def test_upload_complete_fixture_uses_qualified_native_multipart_semantics() -> None:
    soup = BeautifulSoup(_fixture("upload_complete.html"), "html.parser")
    form = soup.select_one("form#application_form")
    submit = soup.select_one("button#submit_app")

    assert form is not None
    assert submit is not None
    assert form.get("method") == "post"
    assert form.get("action") == "/fixture/jobs/123456"
    assert form.get("enctype") == "multipart/form-data"
    assert form.get("target") == "_self"
    assert submit.get("type") == "submit"
    assert submit.get("name") == "commit"
    assert submit.get("value") == "submit_application"


@pytest.mark.parametrize(
    ("outer_attribute", "button_attribute"),
    [
        ("", "disabled"),
        ("", 'aria-disabled="true"'),
        ("inert", ""),
        ('aria-disabled="true"', ""),
        ('style="pointer-events: none"', ""),
        ('style="content-visibility: hidden"', ""),
        ('style="visibility: collapse"', ""),
        ('style="opacity: 0"', ""),
        ("hidden", ""),
    ],
    ids=[
        "disabled",
        "aria-disabled-control",
        "inert-ancestor-outside-form",
        "aria-disabled-ancestor-outside-form",
        "pointer-events-ancestor",
        "content-visibility-ancestor",
        "visibility-collapse-ancestor",
        "opacity-zero-ancestor",
        "hidden-ancestor",
    ],
)
def test_static_readiness_rejects_factually_non_actionable_final_control(
    outer_attribute: str,
    button_attribute: str,
) -> None:
    html = f"""
    <section {outer_attribute}>
      <form id="application_form" data-greenhouse-application method="post"
            action="/fixture/jobs/123456" enctype="multipart/form-data">
        <div data-gh-field data-field-id="resume">
          <label>Resume</label>
          <input name="resume" type="file" required>
        </div>
        <button id="submit_app" type="submit" {button_attribute}>Submit</button>
      </form>
    </section>
    """

    assert assess_greenhouse_v1_snapshot(html).state is not GreenhousePageState.REVIEW


def test_fixture_directory_is_sanitized_and_content_addressable() -> None:
    expected = {
        "already_applied.html",
        "captcha.html",
        "closed_job.html",
        "compliance_consent.html",
        "conditional_expanded.html",
        "conditional_initial.html",
        "duplicate_confirmation.html",
        "embedded_form.html",
        "generic_thank_you.html",
        "hidden_confirmation.html",
        "hosted_custom_questions.html",
        "job_id_form.html",
        "login.html",
        "non_actionable_submit.html",
        "preexisting_confirmation.html",
        "selector_drift.html",
        "submitter_proxy_actionability_drift.html",
        "upload_complete.html",
        "upload_failed.html",
        "upload_pending.html",
        "validation_errors.html",
        "verified_confirmation.html",
    }
    actual = {path.name for path in FIXTURES.iterdir() if path.is_file()}

    assert actual == expected
    for path in FIXTURES.iterdir():
        if path.is_file():
            assert hashlib.sha256(path.read_bytes()).hexdigest()
            text = path.read_text(encoding="utf-8").casefold()
            assert "ali.h" not in text
            assert not re.search(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", text)
            assert "http://" not in text
            assert "https://" not in text
