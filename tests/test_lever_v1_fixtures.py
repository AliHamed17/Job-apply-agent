"""Sanitized fixture contract for Lever browser v1."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.submission_domain import FieldType, ReasonCode, SensitiveCategory
from submitters.lever_identity import parse_lever_posting_identity
from submitters.lever_v1 import (
    LeverAdapterBlockedError,
    LeverPageState,
    assess_lever_v1_snapshot,
    lever_v1_final_action_binding,
    observe_lever_v1_fields,
)

FIXTURES = Path(__file__).parent / "fixtures" / "lever_v1"
POSTING = "11111111-2222-4333-8444-555555555555"
JOB_URL = f"https://jobs.lever.co/sample-company/{POSTING}"
IDENTITY = parse_lever_posting_identity(JOB_URL)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("name", "state", "reason"),
    [
        ("application_basic.html", LeverPageState.FORM, None),
        ("application_custom_select.html", LeverPageState.FORM, None),
        ("application_radio_checkbox.html", LeverPageState.FORM, None),
        ("application_consent.html", LeverPageState.FORM, None),
        ("job_page.html", LeverPageState.JOB, None),
        ("captcha.html", LeverPageState.CHALLENGE, ReasonCode.CHALLENGE_DETECTED),
        ("session_expired.html", LeverPageState.LOGIN, ReasonCode.SESSION_EXPIRED),
        ("mfa.html", LeverPageState.MFA, ReasonCode.MFA_REQUIRED),
        ("closed_job.html", LeverPageState.CLOSED, ReasonCode.JOB_CLOSED),
        ("already_applied.html", LeverPageState.ALREADY_APPLIED, ReasonCode.ALREADY_APPLIED),
        ("selector_drift.html", LeverPageState.SELECTOR_DRIFT, ReasonCode.SELECTOR_DRIFT),
        ("generic_success.html", LeverPageState.SELECTOR_DRIFT, ReasonCode.SELECTOR_DRIFT),
        ("hidden_confirmation.html", LeverPageState.SELECTOR_DRIFT, ReasonCode.SELECTOR_DRIFT),
        ("mismatched_confirmation.html", LeverPageState.SELECTOR_DRIFT, ReasonCode.SELECTOR_DRIFT),
        ("outer_aria_disabled.html", LeverPageState.FORM, None),
        (
            "outer_content_visibility.html",
            LeverPageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        ),
        ("outer_has_proxy_guard.html", LeverPageState.FORM, None),
        ("outer_inert.html", LeverPageState.FORM, None),
        ("outer_pointer_events.html", LeverPageState.FORM, None),
        ("outer_zero_area.html", LeverPageState.FORM, None),
        ("verified_confirmation.html", LeverPageState.CONFIRMATION, None),
    ],
)
def test_fixture_state_contract(
    name: str,
    state: LeverPageState,
    reason: ReasonCode | None,
) -> None:
    assessment = assess_lever_v1_snapshot(
        _fixture(name),
        IDENTITY.apply_url,
        identity=IDENTITY,
    )
    assert assessment.state is state
    assert assessment.reason_code is reason


def test_basic_form_observer_is_ordered_bounded_and_attachment_explicit() -> None:
    fields = observe_lever_v1_fields(
        _fixture("application_basic.html"),
        identity=IDENTITY,
    )

    assert [(field.field_id, field.position) for field in fields] == [
        ("candidate-name", 0),
        ("candidate-email", 1),
        ("candidate-resume", 2),
    ]
    assert [field.field_type for field in fields] == [
        FieldType.TEXT,
        FieldType.EMAIL,
        FieldType.FILE,
    ]
    assert fields[2].canonical_name == "resume"
    assert fields[2].constraints.accepted_file_types == (".pdf", ".docx")
    assert all(field.required for field in fields)


def test_exact_options_and_sensitive_consent_semantics_are_observed() -> None:
    select = observe_lever_v1_fields(
        _fixture("application_custom_select.html"),
        identity=IDENTITY,
    )[0]
    consent = observe_lever_v1_fields(
        _fixture("application_consent.html"),
        identity=IDENTITY,
    )[0]

    assert select.field_type is FieldType.SELECT
    assert [(option.option_id, option.value) for option in select.options] == [
        ("office-haifa", "haifa"),
        ("office-tel-aviv", "tel-aviv"),
    ]
    assert consent.field_type is FieldType.CONSENT
    assert consent.sensitive_category is SensitiveCategory.CONSENT


def test_final_action_binding_accounts_for_every_user_and_system_control() -> None:
    html = _fixture("application_basic.html")
    fields = observe_lever_v1_fields(html, identity=IDENTITY)

    binding = lever_v1_final_action_binding(
        html,
        identity=IDENTITY,
        fields=fields,
    )

    assert len(binding) == 64
    assert all(character in "0123456789abcdef" for character in binding)


@pytest.mark.parametrize("name", ["duplicate_field.html", "multiple_resume.html"])
def test_ambiguous_fields_fail_closed(name: str) -> None:
    with pytest.raises(LeverAdapterBlockedError) as exc_info:
        observe_lever_v1_fields(_fixture(name), identity=IDENTITY)
    assert exc_info.value.reason_code is ReasonCode.SELECTOR_DRIFT


@pytest.mark.parametrize(
    "name",
    [
        "disabled_submit.html",
        "invalid_action.html",
        "outer_aria_disabled.html",
        "outer_inert.html",
        "outer_pointer_events.html",
        "outer_zero_area.html",
        "unreviewed_hidden_control.html",
        "wrong_method.html",
    ],
)
def test_unreviewed_or_invalid_final_boundary_fails_before_plan(name: str) -> None:
    html = _fixture(name)
    fields = observe_lever_v1_fields(html, identity=IDENTITY)

    with pytest.raises(LeverAdapterBlockedError) as exc_info:
        lever_v1_final_action_binding(
            html,
            identity=IDENTITY,
            fields=fields,
        )

    assert exc_info.value.reason_code in {
        ReasonCode.FORM_CHANGED,
        ReasonCode.SELECTOR_DRIFT,
    }


def test_hidden_content_visibility_fails_before_field_observation() -> None:
    with pytest.raises(LeverAdapterBlockedError) as exc_info:
        observe_lever_v1_fields(
            _fixture("outer_content_visibility.html"),
            identity=IDENTITY,
        )

    assert exc_info.value.reason_code is ReasonCode.SELECTOR_DRIFT


def test_css_has_proxy_guard_binds_without_inserting_a_helper_control() -> None:
    html = _fixture("outer_has_proxy_guard.html")
    fields = observe_lever_v1_fields(html, identity=IDENTITY)

    assert ":has([data-proof-proxy])" in html
    assert "data-proof-proxy" not in html.split("</style>", maxsplit=1)[1]
    assert (
        len(
            lever_v1_final_action_binding(
                html,
                identity=IDENTITY,
                fields=fields,
            )
        )
        == 64
    )


def test_fixture_set_is_sanitized_and_contains_no_live_identity() -> None:
    paths = sorted(FIXTURES.glob("*.html"))
    assert len(paths) == 28
    forbidden = (
        "ali.h.",
        "@gmail.com",
        "linkedin.com/in/",
        "nvidia",
        "amazon",
        "apple",
        "C:\\Users\\",
        "/Users/",
    )
    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert len(content.encode("utf-8")) <= 256 * 1024
        assert not any(marker.casefold() in content.casefold() for marker in forbidden)
        assert "sample-company" in content or path.name in {
            "already_applied.html",
            "captcha.html",
            "closed_job.html",
            "generic_success.html",
            "hidden_confirmation.html",
            "job_page.html",
            "mfa.html",
            "mismatched_confirmation.html",
            "outer_aria_disabled.html",
            "outer_content_visibility.html",
            "outer_has_proxy_guard.html",
            "outer_inert.html",
            "outer_pointer_events.html",
            "outer_zero_area.html",
            "selector_drift.html",
            "session_expired.html",
            "verified_confirmation.html",
        }
