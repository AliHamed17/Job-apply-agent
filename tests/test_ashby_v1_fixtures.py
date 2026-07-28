from __future__ import annotations

from pathlib import Path

import pytest

from core.submission_domain import (
    VERIFIED_ATTACHMENT_EVIDENCE_REF,
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    FieldType,
    ReasonCode,
)
from submitters.ashby_identity import parse_ashby_candidate_url
from submitters.ashby_v1 import (
    ASHBY_CONFIRMATION_SELECTOR,
    AshbyAdapterBlockedError,
    AshbyPageState,
    ashby_v1_answer_bindings,
    ashby_v1_final_request_contract,
    ashby_v1_form_fingerprint,
    ashby_v1_validation_reason,
    assess_ashby_v1_snapshot,
    observe_ashby_v1_fields,
)

POSTING = "4f44b0a5-5482-4be6-bc11-3d89040b9fa1"
APPLICATION_URL = f"https://jobs.ashbyhq.com/fixture-board/{POSTING}/application"
FIXTURES = Path(__file__).parent / "fixtures" / "ashby_v1"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_base_fixture_ignores_explicitly_unrendered_conditional_control() -> None:
    html = _fixture("application_base.html")
    fields = observe_ashby_v1_fields(html)

    assert [field.field_id for field in fields] == [
        "full-name",
        "email",
        "preferred-team",
        "resume",
        "privacy-consent",
    ]
    assert [field.position for field in fields] == list(range(5))
    assert fields[2].field_type is FieldType.SELECT
    assert tuple(option.value for option in fields[2].options) == ("ai", "platform")
    assert fields[3].field_type is FieldType.FILE
    assert fields[3].constraints.accepted_file_types == (".pdf", "application/pdf")
    assert fields[4].field_type is FieldType.CONSENT

    identity = parse_ashby_candidate_url(APPLICATION_URL).identity
    contract = ashby_v1_final_request_contract(
        html,
        APPLICATION_URL,
        identity,
        fields,
    )
    assert contract is not None
    assert contract.target_url == APPLICATION_URL
    assert contract.method == "POST"
    assert contract.enctype == "multipart/form-data"
    assert contract.system_controls == ("postingId", "csrfToken")
    assert contract.submit_control == ("action", "submit")
    assert len(ashby_v1_form_fingerprint(fields, contract.digest)) == 64


def test_react_conditional_field_changes_exact_form_contract() -> None:
    base = _fixture("application_base.html")
    expanded = _fixture("application_conditional.html")
    base_fields = observe_ashby_v1_fields(base)
    expanded_fields = observe_ashby_v1_fields(expanded)
    identity = parse_ashby_candidate_url(APPLICATION_URL).identity
    base_contract = ashby_v1_final_request_contract(
        base,
        APPLICATION_URL,
        identity,
        base_fields,
    )
    expanded_contract = ashby_v1_final_request_contract(
        expanded,
        APPLICATION_URL,
        identity,
        expanded_fields,
    )

    assert [field.field_id for field in expanded_fields] == [
        "preferred-team",
        "conditional-detail",
        "resume",
    ]
    assert base_contract is not None
    assert expanded_contract is not None
    assert base_contract.digest != expanded_contract.digest
    assert ashby_v1_form_fingerprint(
        base_fields,
        base_contract.digest,
    ) != ashby_v1_form_fingerprint(expanded_fields, expanded_contract.digest)


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("validation_error.html", ReasonCode.REQUIRED_FIELD_UNKNOWN),
        ("upload_pending.html", ReasonCode.ATTACHMENT_UNVERIFIED),
    ],
)
def test_visible_validation_and_upload_progress_block(name: str, reason: ReasonCode) -> None:
    assert ashby_v1_validation_reason(_fixture(name)) is reason


def test_upload_error_uses_attachment_reason_not_unknown_field_reason() -> None:
    html = _fixture("upload_pending.html").replace(
        'data-ashby-upload-state="uploading"',
        'data-ashby-upload-state="error"',
    )

    assert ashby_v1_validation_reason(html) is ReasonCode.ATTACHMENT_UNVERIFIED


def test_only_exact_visible_confirmation_is_evidence_candidate() -> None:
    confirmation = _fixture("confirmation.html")

    assert (
        assess_ashby_v1_snapshot(confirmation, APPLICATION_URL).state is AshbyPageState.CONFIRMATION
    )
    assert ASHBY_CONFIRMATION_SELECTOR in confirmation.replace("\n", " ") or (
        "data-ashby-application-confirmation" in confirmation
    )
    generic = "<main><h1>Thanks, success!</h1><p>Application received.</p></main>"
    assert assess_ashby_v1_snapshot(generic, APPLICATION_URL).state is AshbyPageState.SELECTOR_DRIFT


def test_exact_form_contract_rejects_unreviewed_or_redirected_controls() -> None:
    html = _fixture("application_base.html")
    identity = parse_ashby_candidate_url(APPLICATION_URL).identity
    fields = observe_ashby_v1_fields(html)

    extra = html.replace(
        '<button type="submit"',
        '<input name="unreviewed" value="secret"><button type="submit"',
    )
    redirected = html.replace(
        'action="/fixture-board/4f44b0a5-5482-4be6-bc11-3d89040b9fa1/application"',
        'action="https://evil.example/collect"',
    )
    assert ashby_v1_final_request_contract(extra, APPLICATION_URL, identity, fields) is None
    assert (
        ashby_v1_final_request_contract(
            redirected,
            APPLICATION_URL,
            identity,
            fields,
        )
        is None
    )
    for override in (
        'formaction="https://evil.example/collect"',
        'formmethod="get"',
        'formenctype="application/x-www-form-urlencoded"',
        'formtarget="_blank"',
        "formnovalidate",
        'type="button"',
    ):
        overridden = html.replace(
            'type="submit" name="action"',
            f'{override} type="submit" name="action"',
        )
        if override == 'type="button"':
            overridden = html.replace('type="submit" name="action"', 'type="button" name="action"')
        assert (
            ashby_v1_final_request_contract(
                overridden,
                APPLICATION_URL,
                identity,
                fields,
            )
            is None
        )


def test_render_marker_must_match_static_visibility() -> None:
    html = _fixture("application_base.html").replace(
        'hidden aria-hidden="true"\n               data-ashby-field',
        "data-ashby-field",
    )
    with pytest.raises(AshbyAdapterBlockedError) as raised:
        observe_ashby_v1_fields(html)

    assert raised.value.reason_code is ReasonCode.FORM_CHANGED


def test_answer_bindings_require_every_reviewed_field_exactly_once() -> None:
    fields = observe_ashby_v1_fields(_fixture("application_conditional.html"))
    cv_hash = "a" * 64
    decisions = (
        AnswerDecisionV1(
            field_id="preferred-team",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.USER_CONFIRMED,
            value="ai",
            evidence_refs=("operator_confirmation:preferred-team",),
        ),
        AnswerDecisionV1(
            field_id="conditional-detail",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.USER_CONFIRMED,
            value="Sanitized reviewed answer",
            evidence_refs=("operator_confirmation:conditional-detail",),
        ),
        AnswerDecisionV1(
            field_id="resume",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
            value=VERIFIED_ATTACHMENT_SENTINEL,
            evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
        ),
    )

    bindings = ashby_v1_answer_bindings(
        fields,
        decisions,
        selected_cv_hash=cv_hash,
    )
    assert [binding.field_id for binding in bindings] == [
        "preferred-team",
        "conditional-detail",
        "resume",
    ]
    with pytest.raises(ValueError, match="ASHBY_ANSWER_BINDING_INVALID"):
        ashby_v1_answer_bindings(
            fields,
            (*decisions, decisions[0]),
            selected_cv_hash=cv_hash,
        )
