"""Focused contract tests for the v4 submission domain kernel."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from core.submission_domain import (
    VERIFIED_ATTACHMENT_EVIDENCE_REF,
    VERIFIED_ATTACHMENT_SENTINEL,
    AlreadyAppliedOutcome,
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    AttemptOutcome,
    AttemptStage,
    ConfirmedSubmittedOutcome,
    DraftOnlyOutcome,
    EvidenceType,
    FailedBeforeCommitOutcome,
    FieldType,
    FinalSubmitPermit,
    FormFieldConstraintsV1,
    FormFieldV1,
    FormOptionV1,
    FormPlanV1,
    NeedsReviewOutcome,
    ReasonCode,
    SensitiveCategory,
    SubmissionEvidence,
    UnknownOutcome,
    parse_commit_outcome,
)
from core.submission_state import (
    InvalidSubmissionStateError,
    allowed_next_stages,
    can_transition,
    project_legacy_status,
    require_transition,
)
from db.models import SubmissionStatus
from llm.contracts import (
    FORM_RESOLUTION_PROMPT_VERSION,
    QUALIFIED_LOCAL_LLM_MODEL,
    QUALIFIED_LOCAL_LLM_PROVIDER,
)
from llm.qualification_registry import load_qualified_local_model

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64
_QUALIFIED_MODEL_DIGEST = load_qualified_local_model().digest
_NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _current_qualification_report(monkeypatch):
    monkeypatch.setattr(
        "llm.qualification_registry.qualified_model_report_is_current",
        lambda: True,
    )


def _text_field(
    *,
    field_id: str = "full_name",
    required: bool = True,
    sensitive_category: SensitiveCategory | None = None,
) -> FormFieldV1:
    return FormFieldV1(
        field_id=field_id,
        canonical_name=field_id,
        label=field_id.replace("_", " ").title(),
        field_type=FieldType.TEXT,
        required=required,
        position=0,
        sensitive_category=sensitive_category,
    )


def _resolved_answer(
    *,
    field_id: str = "full_name",
    value: str = "Candidate",
    provenance: AnswerProvenance = AnswerProvenance.USER_CONFIRMED,
) -> AnswerDecisionV1:
    return AnswerDecisionV1(
        field_id=field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=provenance,
        value=value,
        evidence_refs=("profile:identity:full_name",),
    )


def _form_plan(
    *,
    fields: tuple[FormFieldV1, ...] | None = None,
    decisions: tuple[AnswerDecisionV1, ...] | None = None,
    blockers: tuple[ReasonCode, ...] = (),
    created_at: datetime = _NOW,
    expires_at: datetime | None = None,
    llm_prompt_version: str | None = None,
    llm_model_provider: str | None = None,
    llm_model_name: str | None = None,
    llm_model_digest: str | None = None,
) -> FormPlanV1:
    fields = fields if fields is not None else (_text_field(),)
    decisions = decisions if decisions is not None else (_resolved_answer(),)
    return FormPlanV1(
        plan_id=uuid4(),
        application_id=12,
        application_revision=4,
        adapter_name="greenhouse",
        adapter_version="1.0.0",
        selector_version="2026.07.1",
        form_fingerprint=_HASH_A,
        selected_cv_id="ai-engineer-v3",
        selected_cv_hash=_HASH_B,
        attached_cv_id="ai-engineer-v3",
        attached_cv_hash=_HASH_B,
        attachment_verified=True,
        profile_version=7,
        session_verified_at=created_at,
        created_at=created_at,
        expires_at=expires_at or created_at + timedelta(minutes=30),
        fields=fields,
        decisions=decisions,
        blockers=blockers,
        llm_prompt_version=llm_prompt_version,
        llm_model_provider=llm_model_provider,
        llm_model_name=llm_model_name,
        llm_model_digest=llm_model_digest,
    )


def _permit(plan: FormPlanV1 | None = None, **updates: object) -> FinalSubmitPermit:
    plan = plan or _form_plan()
    values: dict[str, object] = {
        "attempt_id": 91,
        "job_url_hash": _HASH_C,
        "application_revision": plan.application_revision,
        "adapter_name": plan.adapter_name,
        "adapter_version": plan.adapter_version,
        "selector_version": plan.selector_version,
        "form_fingerprint": plan.form_fingerprint,
        "cv_hash": plan.selected_cv_hash,
        "expires_at": plan.expires_at,
        "nonce": "one-use-opaque-nonce",
    }
    values.update(updates)
    return FinalSubmitPermit(**values)


def _evidence(**updates: object) -> SubmissionEvidence:
    values: dict[str, object] = {
        "attempt_id": 91,
        "evidence_type": EvidenceType.EMPLOYER_APPLICATION_ID,
        "employer_application_id": "ATS-12345",
        "form_fingerprint": _HASH_A,
        "attached_cv_hash": _HASH_B,
        "observed_at": _NOW,
        "digest": _HASH_C,
    }
    values.update(updates)
    return SubmissionEvidence(**values)


def test_stage_and_outcome_values_are_the_exact_v4_contract() -> None:
    assert [stage.value for stage in AttemptStage] == [
        "queued",
        "inspecting",
        "preparing",
        "ready",
        "committing",
        "verifying",
        "finished",
    ]
    assert [outcome.value for outcome in AttemptOutcome] == [
        "confirmed_submitted",
        "already_applied",
        "needs_review",
        "unknown",
        "failed_before_commit",
        "draft_only",
        "operator_confirmed",
        "legacy_unverified",
    ]


def test_domain_models_are_frozen_and_reject_unknown_fields() -> None:
    plan = _form_plan()
    with pytest.raises(ValidationError, match="frozen"):
        plan.profile_version = 8  # type: ignore[misc]

    values = plan.model_dump()
    values["raw_page_content"] = "<html>private answers</html>"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        FormPlanV1.model_validate(values)


def test_submission_evidence_reference_matches_database_bound() -> None:
    with pytest.raises(ValidationError, match="at most 255 characters"):
        _evidence(employer_application_id="x" * 256)


def test_submission_evidence_requires_exactly_one_matching_reference() -> None:
    with pytest.raises(ValidationError, match="forbids other references"):
        _evidence(api_receipt_id="unexpected-second-reference")

    with pytest.raises(ValidationError, match="cannot include a typed reference"):
        _evidence(evidence_type=EvidenceType.VISIBLE_POST_CLICK_CONFIRMATION)


def test_form_plan_enforces_a_maximum_thirty_minute_lifetime() -> None:
    with pytest.raises(ValidationError, match="cannot exceed 30 minutes"):
        _form_plan(expires_at=_NOW + timedelta(minutes=30, seconds=1))
    with pytest.raises(ValidationError, match="must be after creation"):
        _form_plan(expires_at=_NOW)


def test_permit_readiness_rejects_future_session_evidence() -> None:
    plan_values = _form_plan().model_dump()
    plan_values["session_verified_at"] = _NOW + timedelta(minutes=10)
    plan = FormPlanV1.model_validate(plan_values)

    assert plan.ready_for_permit
    assert not plan.ready_for_permit_at(_NOW)
    assert plan.ready_for_permit_at(_NOW + timedelta(minutes=10))


def test_form_plan_requires_an_explicit_blocker_for_unresolved_required_fields() -> None:
    abstention = AnswerDecisionV1(
        field_id="full_name",
        disposition=AnswerDisposition.ABSTAINED,
        provenance=AnswerProvenance.ABSTAINED,
        reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
    )
    with pytest.raises(ValidationError, match="REQUIRED_FIELD_UNKNOWN"):
        _form_plan(decisions=(abstention,))

    blocked = _form_plan(
        decisions=(abstention,),
        blockers=(ReasonCode.REQUIRED_FIELD_UNKNOWN,),
    )
    assert blocked.ready_for_permit is False


def test_sensitive_answer_rejects_llm_or_cv_provenance() -> None:
    nationality = _text_field(
        field_id="nationality",
        sensitive_category=SensitiveCategory.NATIONALITY,
    )
    llm_answer = _resolved_answer(
        field_id="nationality",
        value="unsupported",
        provenance=AnswerProvenance.LOCAL_LLM,
    )
    with pytest.raises(ValidationError, match="confirmed operator evidence"):
        _form_plan(fields=(nationality,), decisions=(llm_answer,))

    confirmed = _resolved_answer(
        field_id="nationality",
        value="confirmed",
        provenance=AnswerProvenance.USER_CONFIRMED,
    ).model_copy(update={"evidence_refs": ("profile:user_confirmed:nationality",)})
    assert _form_plan(fields=(nationality,), decisions=(confirmed,)).ready_for_permit

    unsupported = confirmed.model_copy(update={"evidence_refs": ()})
    with pytest.raises(ValidationError, match="at least one evidence reference"):
        _form_plan(fields=(nationality,), decisions=(unsupported,))


def test_non_sensitive_local_llm_answer_requires_exact_model_audit_identity() -> None:
    llm_answer = _resolved_answer(
        value="supported",
        provenance=AnswerProvenance.LOCAL_LLM,
    )

    with pytest.raises(ValidationError, match="audit identity"):
        _form_plan(decisions=(llm_answer,))

    qualified = _form_plan(
        decisions=(llm_answer,),
        llm_prompt_version=FORM_RESOLUTION_PROMPT_VERSION,
        llm_model_provider=QUALIFIED_LOCAL_LLM_PROVIDER,
        llm_model_name=QUALIFIED_LOCAL_LLM_MODEL,
        llm_model_digest=_QUALIFIED_MODEL_DIGEST,
    )
    assert qualified.ready_for_permit

    for overrides in (
        {"llm_prompt_version": "form-resolution-stale"},
        {"llm_model_provider": "openai"},
        {"llm_model_name": "gpt-4o"},
    ):
        identity = {
            "llm_prompt_version": FORM_RESOLUTION_PROMPT_VERSION,
            "llm_model_provider": QUALIFIED_LOCAL_LLM_PROVIDER,
            "llm_model_name": QUALIFIED_LOCAL_LLM_MODEL,
            "llm_model_digest": _QUALIFIED_MODEL_DIGEST,
        }
        identity.update(overrides)
        with pytest.raises(ValidationError, match="qualified prompt and model identity"):
            _form_plan(decisions=(llm_answer,), **identity)


def test_non_llm_answers_reject_orphan_model_audit_metadata() -> None:
    with pytest.raises(ValidationError, match="exactly when local LLM answers exist"):
        _form_plan(
            llm_prompt_version=FORM_RESOLUTION_PROMPT_VERSION,
            llm_model_provider=QUALIFIED_LOCAL_LLM_PROVIDER,
            llm_model_name=QUALIFIED_LOCAL_LLM_MODEL,
            llm_model_digest=_QUALIFIED_MODEL_DIGEST,
        )


@pytest.mark.parametrize(
    ("field_type", "sensitive_category"),
    [
        (FieldType.CONSENT, None),
        (FieldType.CONSENT, SensitiveCategory.ATTESTATION),
        (FieldType.ATTESTATION, None),
        (FieldType.ATTESTATION, SensitiveCategory.CONSENT),
        (FieldType.TEXT, SensitiveCategory.CONSENT),
        (FieldType.CHECKBOX, SensitiveCategory.ATTESTATION),
    ],
)
def test_consent_and_attestation_controls_cannot_bypass_sensitive_policy(
    field_type: FieldType,
    sensitive_category: SensitiveCategory | None,
) -> None:
    with pytest.raises(ValidationError, match="matching|must match"):
        FormFieldV1(
            field_id="legal_control",
            label="Legal control",
            field_type=field_type,
            required=True,
            position=0,
            sensitive_category=sensitive_category,
        )


@pytest.mark.parametrize(
    ("field_type", "sensitive_category"),
    [
        (FieldType.CONSENT, SensitiveCategory.CONSENT),
        (FieldType.ATTESTATION, SensitiveCategory.ATTESTATION),
    ],
)
def test_consent_and_attestation_answers_require_confirmed_evidence(
    field_type: FieldType,
    sensitive_category: SensitiveCategory,
) -> None:
    field = FormFieldV1(
        field_id="legal_control",
        label="Legal control",
        field_type=field_type,
        required=True,
        position=0,
        sensitive_category=sensitive_category,
    )
    llm_answer = _resolved_answer(
        field_id="legal_control",
        value="accepted",
        provenance=AnswerProvenance.LOCAL_LLM,
    )
    with pytest.raises(ValidationError, match="confirmed operator evidence"):
        _form_plan(fields=(field,), decisions=(llm_answer,))


@pytest.mark.parametrize(
    ("field_type", "value", "sensitive_category", "message"),
    [
        (FieldType.CHECKBOX, "false", None, "must be boolean"),
        (FieldType.CONSENT, "accepted", SensitiveCategory.CONSENT, "must be boolean"),
        (FieldType.ATTESTATION, 1, SensitiveCategory.ATTESTATION, "must be boolean"),
        (FieldType.TEXT, True, None, "must be strings"),
        (FieldType.NUMBER, True, None, "finite numeric"),
        (FieldType.NUMBER, "10", None, "finite numeric"),
        (FieldType.SELECT, True, None, "must be strings"),
        (FieldType.MULTI_SELECT, "one", None, "tuple of strings"),
        (FieldType.UNKNOWN, "guess", None, "unknown controls"),
    ],
)
def test_resolved_answers_are_strictly_typed_for_the_observed_control(
    field_type: FieldType,
    value: object,
    sensitive_category: SensitiveCategory | None,
    message: str,
) -> None:
    options = ()
    if field_type in {FieldType.SELECT, FieldType.MULTI_SELECT}:
        options = (FormOptionV1(value="one", label="One"),)
    field = FormFieldV1(
        field_id="typed_control",
        label="Typed control",
        field_type=field_type,
        required=True,
        position=0,
        options=options,
        sensitive_category=sensitive_category,
    )
    answer = AnswerDecisionV1(
        field_id=field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.USER_CONFIRMED,
        value=value,
        evidence_refs=("operator_confirmation:typed-control",),
    )

    with pytest.raises(ValidationError, match=message):
        _form_plan(fields=(field,), decisions=(answer,))


def test_boolean_legal_controls_and_numeric_constraints_are_enforced() -> None:
    consent = FormFieldV1(
        field_id="consent",
        label="Consent",
        field_type=FieldType.CONSENT,
        required=True,
        position=0,
        sensitive_category=SensitiveCategory.CONSENT,
    )
    accepted = AnswerDecisionV1(
        field_id="consent",
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.USER_CONFIRMED,
        value=True,
        evidence_refs=("operator_confirmation:consent-v1",),
    )
    assert _form_plan(fields=(consent,), decisions=(accepted,)).ready_for_permit

    years = FormFieldV1(
        field_id="years",
        label="Years",
        field_type=FieldType.NUMBER,
        required=True,
        position=0,
        constraints=FormFieldConstraintsV1(min_value=0, max_value=50),
    )
    for invalid in (-1, 51, float("inf")):
        decision = AnswerDecisionV1(
            field_id="years",
            disposition=AnswerDisposition.RESOLVED,
            provenance=AnswerProvenance.CV_EVIDENCE,
            value=invalid,
        )
        with pytest.raises(ValidationError, match="minimum|maximum|finite"):
            _form_plan(fields=(years,), decisions=(decision,))


def test_verified_resume_attachment_uses_only_non_path_sentinel() -> None:
    field = FormFieldV1(
        field_id="resume-upload",
        canonical_name="resume_upload",
        label="Upload your resume",
        field_type=FieldType.FILE,
        required=True,
        position=0,
        constraints=FormFieldConstraintsV1(
            accepted_file_types=("application/pdf",),
        ),
    )
    decision = AnswerDecisionV1(
        field_id=field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
        value=VERIFIED_ATTACHMENT_SENTINEL,
        evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
    )

    plan = _form_plan(fields=(field,), decisions=(decision,))

    assert plan.ready_for_permit
    assert _HASH_B not in str(decision.model_dump())
    assert "ai-engineer" not in str(decision.model_dump())


def test_file_control_rejects_arbitrary_path_or_operator_string() -> None:
    field = FormFieldV1(
        field_id="resume-upload",
        canonical_name="resume_upload",
        label="Resume upload",
        field_type=FieldType.FILE,
        required=True,
        position=0,
    )
    decision = AnswerDecisionV1(
        field_id=field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.USER_CONFIRMED,
        value=r"C:\private\candidate.pdf",
        evidence_refs=("operator_confirmation:review-1",),
    )

    with pytest.raises(ValidationError, match="only verified attachment provenance"):
        _form_plan(fields=(field,), decisions=(decision,))


def test_verified_attachment_provenance_rejects_non_file_or_mismatched_metadata() -> None:
    text_field = _text_field(field_id="resume")
    non_file_decision = AnswerDecisionV1(
        field_id=text_field.field_id,
        disposition=AnswerDisposition.RESOLVED,
        provenance=AnswerProvenance.VERIFIED_ATTACHMENT,
        value=VERIFIED_ATTACHMENT_SENTINEL,
        evidence_refs=(VERIFIED_ATTACHMENT_EVIDENCE_REF,),
    )
    with pytest.raises(ValidationError, match="exact reviewed attachment metadata"):
        _form_plan(fields=(text_field,), decisions=(non_file_decision,))

    file_field = FormFieldV1(
        field_id="resume-upload",
        canonical_name="resume_upload",
        label="Resume upload",
        field_type=FieldType.FILE,
        required=True,
        position=0,
    )
    file_decision = non_file_decision.model_copy(update={"field_id": file_field.field_id})
    valid_plan = _form_plan(fields=(file_field,), decisions=(file_decision,))
    mismatched = valid_plan.model_dump(mode="json")
    mismatched["attached_cv_hash"] = _HASH_C

    with pytest.raises(ValidationError, match="exact reviewed attachment metadata"):
        FormPlanV1.model_validate(mismatched)


@pytest.mark.parametrize(
    "options",
    [
        (
            {"value": "A", "label": "Alpha"},
            {"value": "a", "label": "Beta"},
        ),
        (
            {"value": "a", "label": "Yes"},
            {"value": "b", "label": " yes "},
        ),
        (
            {"value": "yes", "label": "Allowed"},
            {"value": "b", "label": "YES"},
        ),
        (
            {"option_id": "choice-a", "value": "a", "label": "Alpha"},
            {"option_id": "CHOICE-A", "value": "b", "label": "Beta"},
        ),
    ],
)
def test_option_contract_rejects_normalized_or_cross_alias_ambiguity(options) -> None:
    with pytest.raises(
        ValidationError,
        match="option IDs|normalized option values and labels",
    ):
        FormFieldV1(
            field_id="ambiguous-options",
            label="Choose one",
            field_type=FieldType.SELECT,
            required=True,
            position=0,
            options=options,
        )


def test_form_collection_bounds_accept_boundary_and_reject_one_over() -> None:
    boundary_options = tuple(
        FormOptionV1(value=f"value-{index}", label=f"Option {index}") for index in range(200)
    )
    field = FormFieldV1(
        field_id="bounded-options",
        label="Choose one",
        field_type=FieldType.SELECT,
        required=False,
        position=0,
        options=boundary_options,
    )
    assert len(field.options) == 200

    with pytest.raises(ValidationError, match="at most 200"):
        FormFieldV1(
            field_id="too-many-options",
            label="Choose one",
            field_type=FieldType.SELECT,
            required=False,
            position=0,
            options=(
                *boundary_options,
                FormOptionV1(value="overflow", label="Overflow"),
            ),
        )

    boundary_types = tuple(f"application/x-type-{index}" for index in range(32))
    assert len(FormFieldConstraintsV1(accepted_file_types=boundary_types).accepted_file_types) == 32
    with pytest.raises(ValidationError, match="at most 32"):
        FormFieldConstraintsV1(
            accepted_file_types=(*boundary_types, "application/x-overflow"),
        )


def test_form_plan_field_and_total_serialized_size_bounds() -> None:
    fields = tuple(
        FormFieldV1(
            field_id=f"optional-{index}",
            label=f"Optional field {index}",
            field_type=FieldType.TEXT,
            required=False,
            position=index,
        )
        for index in range(200)
    )
    decisions = tuple(
        AnswerDecisionV1(
            field_id=field.field_id,
            disposition=AnswerDisposition.ABSTAINED,
            provenance=AnswerProvenance.ABSTAINED,
            reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
        )
        for field in fields
    )
    assert len(_form_plan(fields=fields, decisions=decisions).fields) == 200

    overflow_field = FormFieldV1(
        field_id="optional-overflow",
        label="Optional overflow",
        field_type=FieldType.TEXT,
        required=False,
        position=200,
    )
    overflow_decision = AnswerDecisionV1(
        field_id=overflow_field.field_id,
        disposition=AnswerDisposition.ABSTAINED,
        provenance=AnswerProvenance.ABSTAINED,
        reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN,
    )
    with pytest.raises(ValidationError, match="at most 200"):
        _form_plan(
            fields=(*fields, overflow_field),
            decisions=(*decisions, overflow_decision),
        )

    oversized_fields = tuple(
        field.model_copy(update={"label": f"Field {index} " + ("x" * 1_900)})
        for index, field in enumerate(fields)
    )
    with pytest.raises(ValidationError, match="serialized form plan"):
        _form_plan(fields=oversized_fields, decisions=decisions)


def test_text_constraints_use_bounded_safe_pattern_validation() -> None:
    field = FormFieldV1(
        field_id="employee_code",
        label="Employee code",
        field_type=FieldType.TEXT,
        required=True,
        position=0,
        constraints=FormFieldConstraintsV1(
            min_length=4,
            max_length=8,
            pattern=r"[A-Z]{2}\d{2,6}",
        ),
    )
    valid = _resolved_answer(field_id="employee_code", value="AB1234")
    assert _form_plan(fields=(field,), decisions=(valid,)).ready_for_permit

    for invalid in ("A1", "ab1234", "AB1234567"):
        decision = _resolved_answer(field_id="employee_code", value=invalid)
        with pytest.raises(ValidationError, match="minimum|maximum|safe observed pattern"):
            _form_plan(fields=(field,), decisions=(decision,))

    unsupported_pattern = field.model_copy(
        update={
            "constraints": FormFieldConstraintsV1(
                pattern=r"(a+)+$",
            )
        }
    )
    with pytest.raises(ValidationError, match="safe observed pattern"):
        _form_plan(
            fields=(unsupported_pattern,),
            decisions=(_resolved_answer(field_id="employee_code", value="aaaa"),),
        )


@pytest.mark.parametrize("field_name", ["min_value", "max_value"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_numeric_form_constraints_are_rejected(field_name, value) -> None:
    with pytest.raises(ValidationError, match="finite"):
        FormFieldConstraintsV1.model_validate({field_name: value})


@pytest.mark.parametrize(
    ("field_type", "valid", "invalid"),
    [
        (FieldType.DATE, "2026-07-26", "tomorrow-ish"),
        (FieldType.DATE, "2024-02-29", "2023-02-29"),
        (FieldType.EMAIL, "candidate@example.test", "not-an-email"),
        (FieldType.PHONE, "+1 (555) 123-4567", "abc"),
        (FieldType.URL, "https://example.test/profile", "definitely not a url"),
        (FieldType.URL, "http://localhost:8000/path", "javascript:alert(1)"),
    ],
)
def test_typed_string_controls_require_canonical_semantic_values(
    field_type,
    valid,
    invalid,
) -> None:
    field = FormFieldV1(
        field_id=f"typed-{field_type.value}",
        label=field_type.value,
        field_type=field_type,
        required=True,
        position=0,
    )
    assert _form_plan(
        fields=(field,),
        decisions=(_resolved_answer(field_id=field.field_id, value=valid),),
    ).ready_for_permit

    with pytest.raises(ValidationError, match="valid canonical value"):
        _form_plan(
            fields=(field,),
            decisions=(_resolved_answer(field_id=field.field_id, value=invalid),),
        )


@pytest.mark.parametrize(
    ("pattern", "value"),
    [
        (r"^[A-Z]{2}\d{2,6}$", "AB1234"),
        (r"^\+?\d{10,15}$", "+15551234567"),
        (r"[A-Za-z0-9._-]{1,64}", "applicant_01"),
        (r"\d{5}-?\d{0,4}", "12345-6789"),
        (r"\$", "$"),
    ],
)
def test_safe_common_form_patterns_use_the_finite_matcher(pattern, value) -> None:
    field = FormFieldV1(
        field_id="bounded-pattern",
        label="Bounded pattern",
        field_type=FieldType.TEXT,
        required=True,
        position=0,
        constraints=FormFieldConstraintsV1(pattern=pattern),
    )

    assert _form_plan(
        fields=(field,),
        decisions=(_resolved_answer(field_id=field.field_id, value=value),),
    ).ready_for_permit


@pytest.mark.parametrize(
    "pattern",
    [
        r"(a+)+$",
        r"(a|aa)+$",
        r"([A-Z]*)*$",
        r"(?:a){1,3}",
        r"(?=a)a",
        r"(a)\1",
        r"a*a*a*a*a*a*a*a*a*a*",
        r"[a-zA-Z]+",
        r"a{1,64}a{1,64}a{1,64}a{1,64}a{1,64}",
        r"[^a]{1,10}",
        r"\D{1,10}",
    ],
)
def test_untrusted_regex_features_are_rejected_without_regex_execution(pattern) -> None:
    field = FormFieldV1(
        field_id="unsafe-pattern",
        label="Unsafe pattern",
        field_type=FieldType.TEXT,
        required=True,
        position=0,
        constraints=FormFieldConstraintsV1(pattern=pattern),
    )

    with pytest.raises(ValidationError, match="safe observed pattern"):
        _form_plan(
            fields=(field,),
            decisions=(
                _resolved_answer(
                    field_id=field.field_id,
                    value="a" * 2_000,
                ),
            ),
        )


def test_form_plan_validates_exact_enabled_option_values() -> None:
    field = FormFieldV1(
        field_id="work_mode",
        label="Work mode",
        field_type=FieldType.SELECT,
        required=True,
        position=0,
        options=(
            FormOptionV1(value="hybrid", label="Hybrid"),
            FormOptionV1(value="onsite", label="On site", disabled=True),
        ),
    )
    invalid = _resolved_answer(field_id="work_mode", value="onsite")
    with pytest.raises(ValidationError, match="enabled observed option"):
        _form_plan(fields=(field,), decisions=(invalid,))

    valid = _resolved_answer(field_id="work_mode", value="hybrid")
    assert _form_plan(fields=(field,), decisions=(valid,)).ready_for_permit


def test_permit_is_bound_to_the_reviewed_revision_adapter_form_and_cv() -> None:
    plan = _form_plan()
    permit = _permit(plan)
    assert permit.binds(plan)
    assert permit.is_expired(plan.expires_at)
    assert not permit.is_expired(plan.expires_at - timedelta(microseconds=1))

    changed_plan_values = plan.model_dump()
    changed_plan_values["form_fingerprint"] = _HASH_C
    changed_plan = FormPlanV1.model_validate(changed_plan_values)
    assert not permit.binds(changed_plan)


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("attachment_verified", False),
        ("attached_cv_id", "different-cv"),
        ("attached_cv_hash", _HASH_C),
        ("session_verified_at", _NOW - timedelta(seconds=1)),
    ],
)
def test_permit_readiness_requires_exact_attachment_and_current_session(
    field_name: str, field_value: object
) -> None:
    plan_values = _form_plan().model_dump()
    plan_values[field_name] = field_value
    plan = FormPlanV1.model_validate(plan_values)

    assert plan.ready_for_permit is False
    assert _permit(plan).binds(plan) is False


def test_expiry_checks_refuse_naive_datetimes() -> None:
    plan = _form_plan()
    with pytest.raises(ValueError, match="timezone-aware"):
        plan.is_expired(datetime(2026, 7, 26, 9, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        _permit(plan).is_expired(datetime(2026, 7, 26, 9, 0))


@pytest.mark.parametrize(
    ("evidence_type", "reference_name", "reference_value"),
    [
        (EvidenceType.EMPLOYER_APPLICATION_ID, "employer_application_id", "ATS-1"),
        (EvidenceType.API_RECEIPT, "api_receipt_id", "receipt-1"),
        (
            EvidenceType.CANDIDATE_PORTAL_RECORD,
            "candidate_portal_reference",
            "portal-record-1",
        ),
    ],
)
def test_evidence_requires_the_reference_for_its_declared_type(
    evidence_type: EvidenceType,
    reference_name: str,
    reference_value: str,
) -> None:
    values = _evidence().model_dump()
    values.update(
        {
            "evidence_type": evidence_type,
            "employer_application_id": None,
            "api_receipt_id": None,
            "candidate_portal_reference": None,
        }
    )
    with pytest.raises(ValidationError, match="requires its typed reference"):
        SubmissionEvidence.model_validate(values)

    values[reference_name] = reference_value
    assert SubmissionEvidence.model_validate(values).evidence_type == evidence_type


def test_evidence_is_redacted_and_requires_exact_form_and_cv_hashes() -> None:
    visible = _evidence(
        evidence_type=EvidenceType.VISIBLE_POST_CLICK_CONFIRMATION,
        employer_application_id=None,
    )
    assert visible.form_fingerprint == _HASH_A
    assert visible.attached_cv_hash == _HASH_B

    values = visible.model_dump()
    values["field_answers"] = {"nationality": "private"}
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SubmissionEvidence.model_validate(values)


def test_commit_outcome_is_discriminated_and_has_no_success_boolean() -> None:
    outcome = parse_commit_outcome(
        {
            "kind": "confirmed_submitted",
            "evidence": _evidence().model_dump(mode="json"),
        }
    )
    assert isinstance(outcome, ConfirmedSubmittedOutcome)

    contradictory = {
        "kind": "draft_only",
        "reason_code": "DRY_RUN_DISCARDED",
        "success": True,
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_commit_outcome(contradictory)
    with pytest.raises(ValidationError):
        parse_commit_outcome({"kind": "confirmed_submitted"})


def test_commit_outcomes_reject_incompatible_reason_codes() -> None:
    with pytest.raises(ValidationError, match="needs-review"):
        NeedsReviewOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
    with pytest.raises(ValidationError, match="unknown outcome"):
        UnknownOutcome(reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN)
    with pytest.raises(ValidationError, match="failed-before-commit"):
        FailedBeforeCommitOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)


def test_transition_table_is_forward_only_and_requires_terminal_outcomes() -> None:
    assert allowed_next_stages(AttemptStage.READY) == frozenset(
        {AttemptStage.COMMITTING, AttemptStage.FINISHED}
    )
    assert can_transition(AttemptStage.QUEUED, AttemptStage.INSPECTING)
    assert can_transition(AttemptStage.INSPECTING, AttemptStage.READY)
    assert not can_transition(AttemptStage.READY, AttemptStage.PREPARING)
    assert not can_transition(AttemptStage.READY, AttemptStage.READY)
    assert not can_transition(
        AttemptStage.READY,
        AttemptStage.COMMITTING,
        FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY),
    )
    assert not can_transition(AttemptStage.READY, AttemptStage.FINISHED)


def test_ambiguity_boundary_only_allows_unknown_until_verification() -> None:
    unknown = UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED)
    failed = FailedBeforeCommitOutcome(reason_code=ReasonCode.NETWORK_ERROR)
    confirmed = ConfirmedSubmittedOutcome(evidence=_evidence())

    assert can_transition(AttemptStage.COMMITTING, AttemptStage.FINISHED, unknown)
    assert not can_transition(AttemptStage.COMMITTING, AttemptStage.FINISHED, failed)
    assert not can_transition(AttemptStage.COMMITTING, AttemptStage.FINISHED, confirmed)
    assert can_transition(AttemptStage.VERIFYING, AttemptStage.FINISHED, confirmed)
    assert can_transition(AttemptStage.VERIFYING, AttemptStage.FINISHED, unknown)


def test_operator_and_legacy_outcomes_are_not_live_commit_transitions() -> None:
    assert not can_transition(
        AttemptStage.VERIFYING,
        AttemptStage.FINISHED,
        AttemptOutcome.OPERATOR_CONFIRMED,
    )
    assert not can_transition(
        AttemptStage.VERIFYING,
        AttemptStage.FINISHED,
        AttemptOutcome.LEGACY_UNVERIFIED,
    )


def test_require_transition_raises_for_unlisted_transition() -> None:
    with pytest.raises(InvalidSubmissionStateError, match="ready -> preparing"):
        require_transition(AttemptStage.READY, AttemptStage.PREPARING)


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        (AttemptStage.QUEUED, SubmissionStatus.PENDING),
        (AttemptStage.INSPECTING, SubmissionStatus.PENDING),
        (AttemptStage.PREPARING, SubmissionStatus.PENDING),
        (AttemptStage.READY, SubmissionStatus.PENDING),
        (AttemptStage.COMMITTING, SubmissionStatus.RUNNING),
        (AttemptStage.VERIFYING, SubmissionStatus.RUNNING),
    ],
)
def test_active_stages_project_to_non_green_legacy_status(
    stage: AttemptStage, expected: SubmissionStatus
) -> None:
    assert project_legacy_status(stage) == expected


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (ConfirmedSubmittedOutcome(evidence=_evidence()), SubmissionStatus.SUCCESS),
        (AlreadyAppliedOutcome(), SubmissionStatus.FAILED),
        (
            NeedsReviewOutcome(reason_code=ReasonCode.REQUIRED_FIELD_UNKNOWN),
            SubmissionStatus.FAILED,
        ),
        (
            UnknownOutcome(reason_code=ReasonCode.FINAL_ACTION_UNCONFIRMED),
            SubmissionStatus.UNKNOWN,
        ),
        (
            FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY),
            SubmissionStatus.FAILED,
        ),
        (DraftOnlyOutcome(), SubmissionStatus.DRAFT_ONLY),
        (AttemptOutcome.OPERATOR_CONFIRMED, SubmissionStatus.UNKNOWN),
        (AttemptOutcome.LEGACY_UNVERIFIED, SubmissionStatus.UNKNOWN),
    ],
)
def test_terminal_projection_reserves_green_for_employer_evidence(
    outcome: object, expected: SubmissionStatus
) -> None:
    assert project_legacy_status(AttemptStage.FINISHED, outcome) == expected  # type: ignore[arg-type]


def test_projection_fails_closed_for_contradictory_stage_and_outcome() -> None:
    with pytest.raises(InvalidSubmissionStateError, match="finished attempts require"):
        project_legacy_status(AttemptStage.FINISHED)
    with pytest.raises(InvalidSubmissionStateError, match="non-finished attempts"):
        project_legacy_status(
            AttemptStage.QUEUED,
            FailedBeforeCommitOutcome(reason_code=ReasonCode.RUNTIME_NOT_READY),
        )
