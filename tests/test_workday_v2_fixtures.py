"""Sanitized fixture qualification for the exact Workday v2 selectors."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.submission_domain import FieldType, ReasonCode
from submitters.workday_v2 import (
    WORKDAY_CONFIRMATION_SELECTOR,
    WorkdayPageState,
    assess_workday_v2_snapshot,
    observe_workday_v2_fields,
    workday_v2_form_fingerprint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "workday_v2"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("fixture_name", "expected_state", "expected_reason"),
    [
        ("login.html", WorkdayPageState.LOGIN, ReasonCode.SESSION_EXPIRED),
        ("mfa.html", WorkdayPageState.MFA, ReasonCode.MFA_REQUIRED),
        ("captcha.html", WorkdayPageState.CHALLENGE, ReasonCode.CHALLENGE_DETECTED),
        ("closed_job.html", WorkdayPageState.CLOSED, ReasonCode.JOB_CLOSED),
        (
            "already_applied.html",
            WorkdayPageState.ALREADY_APPLIED,
            ReasonCode.ALREADY_APPLIED,
        ),
        ("review.html", WorkdayPageState.REVIEW, None),
        ("resume_upload.html", WorkdayPageState.RESUME_UPLOAD, None),
        (
            "selector_drift.html",
            WorkdayPageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        ),
        ("verified_confirmation.html", WorkdayPageState.CONFIRMATION, None),
    ],
)
def test_exact_fixture_state_and_reason_contract(
    fixture_name: str,
    expected_state: WorkdayPageState,
    expected_reason: ReasonCode | None,
) -> None:
    assessment = assess_workday_v2_snapshot(_fixture(fixture_name))

    assert assessment.state is expected_state
    assert assessment.reason_code is expected_reason


def test_fixture_set_is_exact_sanitized_and_content_addressed() -> None:
    names = sorted(path.name for path in FIXTURES.glob("*.html"))

    assert names == [
        "already_applied.html",
        "captcha.html",
        "closed_job.html",
        "login.html",
        "mfa.html",
        "resume_upload.html",
        "review.html",
        "selector_drift.html",
        "verified_confirmation.html",
    ]
    for name in names:
        raw = (FIXTURES / name).read_bytes()
        assert len(hashlib.sha256(raw).hexdigest()) == 64
        low = raw.lower()
        assert b"@" not in raw
        assert b"cookie" not in low
        assert b"authorization:" not in low
        assert b"bearer " not in low


def test_resume_fixture_exposes_bounded_exact_field_contract() -> None:
    fields = observe_workday_v2_fields(_fixture("resume_upload.html"))

    assert [(field.field_id, field.field_type, field.required) for field in fields] == [
        ("resume", FieldType.FILE, True),
        ("first_name", FieldType.TEXT, True),
        ("contact_email", FieldType.EMAIL, True),
    ]
    assert fields[0].constraints.accepted_file_types == (".pdf", "application/pdf")
    assert len(workday_v2_form_fingerprint(fields)) == 64
    assert workday_v2_form_fingerprint(fields) == workday_v2_form_fingerprint(fields)


def test_observer_captures_bounded_numeric_and_pattern_constraints() -> None:
    fields = observe_workday_v2_fields(
        """
        <div data-automation-id="formField" data-field-id="years">
          <label for="years">Years of experience</label>
          <input id="years" type="number" min="0" max="80">
        </div>
        <div data-automation-id="formField" data-field-id="code">
          <label for="code">Fixture code</label>
          <input id="code" type="text" minlength="2" maxlength="2"
                 pattern="^[0-9]{2}$">
        </div>
        """
    )

    assert fields[0].constraints.min_value == 0
    assert fields[0].constraints.max_value == 80
    assert fields[1].constraints.min_length == 2
    assert fields[1].constraints.max_length == 2
    assert fields[1].constraints.pattern == "^[0-9]{2}$"


@pytest.mark.parametrize(
    "html",
    [
        "<main><h1>Application submitted successfully</h1></main>",
        "<main><p>Thank you. We received your application.</p></main>",
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
def test_generic_or_incomplete_confirmation_never_qualifies(html: str) -> None:
    assessment = assess_workday_v2_snapshot(html)

    assert assessment.state is WorkdayPageState.SELECTOR_DRIFT
    assert assessment.reason_code is ReasonCode.SELECTOR_DRIFT
    assert WORKDAY_CONFIRMATION_SELECTOR not in html
