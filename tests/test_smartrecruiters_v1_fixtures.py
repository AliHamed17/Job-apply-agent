"""Sanitized fixture qualification for SmartRecruiters candidate v1."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from core.submission_domain import (
    DisclosureKind,
    DisclosureSource,
    FieldType,
    ReasonCode,
    SensitiveCategory,
)
from submitters.smartrecruiters_identity import (
    parse_smartrecruiters_candidate_identity,
    resolve_smartrecruiters_posting_identity,
)
from submitters.smartrecruiters_v1 import (
    SMARTRECRUITERS_CONFIRMATION_SELECTOR,
    SmartRecruitersAdapterBlockedError,
    SmartRecruitersPageState,
    assess_smartrecruiters_v1_snapshot,
    observe_smartrecruiters_v1_disclosures,
    observe_smartrecruiters_v1_fields,
    smartrecruiters_v1_final_action_binding,
    smartrecruiters_v1_form_fingerprint,
)

FIXTURES = Path(__file__).parent / "fixtures" / "smartrecruiters_v1"
JOB_URL = "https://jobs.smartrecruiters.com/FixtureCo/123456789-sanitized-role"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _identity(fixture: str = "application_form.html"):
    candidate = parse_smartrecruiters_candidate_identity(JOB_URL)
    return resolve_smartrecruiters_posting_identity(_fixture(fixture), candidate)


@pytest.mark.parametrize(
    ("fixture", "state", "reason"),
    [
        ("candidate_job.html", SmartRecruitersPageState.JOB, None),
        ("application_form.html", SmartRecruitersPageState.FORM, None),
        ("login.html", SmartRecruitersPageState.LOGIN, ReasonCode.SESSION_EXPIRED),
        ("mfa.html", SmartRecruitersPageState.MFA, ReasonCode.MFA_REQUIRED),
        ("captcha.html", SmartRecruitersPageState.CHALLENGE, ReasonCode.CHALLENGE_DETECTED),
        ("closed_job.html", SmartRecruitersPageState.CLOSED, ReasonCode.JOB_CLOSED),
        (
            "already_applied.html",
            SmartRecruitersPageState.ALREADY_APPLIED,
            ReasonCode.ALREADY_APPLIED,
        ),
        (
            "selector_drift.html",
            SmartRecruitersPageState.SELECTOR_DRIFT,
            ReasonCode.SELECTOR_DRIFT,
        ),
    ],
)
def test_bounded_page_states(fixture, state, reason) -> None:
    identity = _identity()
    assessment = assess_smartrecruiters_v1_snapshot(
        _fixture(fixture),
        JOB_URL,
        identity=identity,
    )

    assert assessment.state is state
    assert assessment.reason_code is reason


def test_verified_confirmation_is_exact_visible_and_posting_bound() -> None:
    identity = _identity()
    assessment = assess_smartrecruiters_v1_snapshot(
        _fixture("verified_confirmation.html"),
        JOB_URL,
        identity=identity,
    )

    assert assessment.state is SmartRecruitersPageState.CONFIRMATION
    for fixture in ("generic_success.html", "hidden_confirmation.html"):
        rejected = assess_smartrecruiters_v1_snapshot(
            _fixture(fixture),
            JOB_URL,
            identity=identity,
        )
        assert rejected.state is SmartRecruitersPageState.SELECTOR_DRIFT
        assert rejected.reason_code is ReasonCode.SELECTOR_DRIFT
    assert SMARTRECRUITERS_CONFIRMATION_SELECTOR not in _fixture("generic_success.html")


def test_form_observer_preserves_global_repeat_conditional_and_sensitive_order() -> None:
    html = _fixture("application_form.html")
    identity = _identity()
    fields = observe_smartrecruiters_v1_fields(html, identity=identity)

    assert [field.field_id for field in fields] == [
        "first_name",
        "resume",
        "screening_level",
        "work_0",
        "work_1",
        "conditional_detail",
        "diversity_choice",
        "privacy_consent",
    ]
    assert fields[1].field_type is FieldType.FILE
    assert fields[2].field_type is FieldType.SELECT
    assert [option.value for option in fields[2].options] == ["one", "two"]
    assert fields[6].sensitive_category is SensitiveCategory.DEMOGRAPHIC
    assert fields[7].field_type is FieldType.CONSENT
    assert fields[7].sensitive_category is SensitiveCategory.CONSENT

    disclosures = observe_smartrecruiters_v1_disclosures(
        html,
        identity=identity,
    )
    assert [item.kind for item in disclosures] == [
        DisclosureKind.PRIVACY_POLICY,
        DisclosureKind.AI_DISCLOSURE,
        DisclosureKind.IMPRINT,
        DisclosureKind.DIVERSITY,
        DisclosureKind.INFORMATION,
    ]
    assert disclosures[0].source is DisclosureSource.LINK
    assert disclosures[0].acknowledgement_field_id == "privacy_consent"
    binding = smartrecruiters_v1_final_action_binding(
        html,
        identity=identity,
        fields=fields,
        disclosures=disclosures,
    )
    assert len(binding) == 64
    assert smartrecruiters_v1_form_fingerprint(
        identity,
        fields,
        disclosures,
        binding,
    ) == smartrecruiters_v1_form_fingerprint(
        identity,
        fields,
        disclosures,
        binding,
    )


def test_missing_privacy_policy_gets_explicit_synthetic_notice() -> None:
    disclosures = observe_smartrecruiters_v1_disclosures(
        _fixture("no_privacy_policy.html"),
        identity=_identity("no_privacy_policy.html"),
    )

    assert len(disclosures) == 1
    assert disclosures[0].kind is DisclosureKind.NO_PRIVACY_POLICY_NOTICE
    assert disclosures[0].source is DisclosureSource.SYNTHETIC
    assert "No privacy policy" in disclosures[0].summary


def test_disclosures_outside_the_exact_candidate_form_cannot_enter_review() -> None:
    baseline = _fixture("application_form.html")
    identity = _identity()
    injected = baseline.replace(
        "</body>",
        (
            '<aside data-qa="form-disclosure" data-disclosure-id="outside" '
            'data-disclosure-kind="information" data-disclosure-source="inline">'
            '<p data-qa="disclosure-summary">Outside spoof</p></aside></body>'
        ),
    )

    assert observe_smartrecruiters_v1_disclosures(
        injected,
        identity=identity,
    ) == observe_smartrecruiters_v1_disclosures(
        baseline,
        identity=identity,
    )


def test_options_require_stable_ids_and_structural_metadata_is_consistent() -> None:
    html = _fixture("application_form.html")
    identity = _identity()
    missing_option_id = html.replace('data-option-id="level-one" ', "", 1)
    with pytest.raises(SmartRecruitersAdapterBlockedError) as exc_info:
        observe_smartrecruiters_v1_fields(
            missing_option_id,
            identity=identity,
        )
    assert exc_info.value.reason_code is ReasonCode.SELECTOR_DRIFT

    for drifted in (
        html.replace('data-repeat-index="1"', 'data-repeat-index="0"', 1),
        html.replace(
            'data-conditional-parent="screening_level"',
            'data-conditional-parent="missing_parent"',
            1,
        ),
        html.replace(
            'name="postingUuid" value="11111111-2222-4333-8444-555555555555"',
            'name="postingUuid" value="99999999-2222-4333-8444-555555555555"',
            1,
        ),
    ):
        fields = observe_smartrecruiters_v1_fields(drifted, identity=identity)
        disclosures = observe_smartrecruiters_v1_disclosures(
            drifted,
            identity=identity,
        )
        with pytest.raises(SmartRecruitersAdapterBlockedError) as exc_info:
            smartrecruiters_v1_final_action_binding(
                drifted,
                identity=identity,
                fields=fields,
                disclosures=disclosures,
            )
        assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED


def test_invalid_candidate_action_and_conditional_drift_change_the_binding() -> None:
    identity = _identity()
    invalid = _fixture("invalid_action.html")
    invalid_fields = observe_smartrecruiters_v1_fields(invalid, identity=identity)
    invalid_disclosures = observe_smartrecruiters_v1_disclosures(
        invalid,
        identity=identity,
    )
    with pytest.raises(SmartRecruitersAdapterBlockedError) as exc_info:
        smartrecruiters_v1_final_action_binding(
            invalid,
            identity=identity,
            fields=invalid_fields,
            disclosures=invalid_disclosures,
        )
    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED

    baseline = _fixture("no_privacy_policy.html")
    drifted = _fixture("conditional_drift.html")
    baseline_fields = observe_smartrecruiters_v1_fields(baseline, identity=identity)
    drifted_fields = observe_smartrecruiters_v1_fields(drifted, identity=identity)
    assert baseline_fields != drifted_fields


def test_fixture_set_contains_no_private_or_authorization_material() -> None:
    names = sorted(path.name for path in FIXTURES.glob("*.html"))
    assert names == [
        "already_applied.html",
        "application_form.html",
        "candidate_job.html",
        "captcha.html",
        "closed_job.html",
        "conditional_drift.html",
        "generic_success.html",
        "hidden_confirmation.html",
        "invalid_action.html",
        "login.html",
        "mfa.html",
        "no_privacy_policy.html",
        "outer_has_proxy_guard.html",
        "selector_drift.html",
        "verified_confirmation.html",
    ]
    for name in names:
        raw = (FIXTURES / name).read_bytes()
        assert len(hashlib.sha256(raw).hexdigest()) == 64
        lowered = raw.lower()
        assert b"@" not in raw
        assert b"cookie" not in lowered
        assert b"authorization:" not in lowered
        assert b"bearer " not in lowered
