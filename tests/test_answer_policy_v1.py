from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from profile.models import UserProfile

import pytest

from core.submission_domain import (
    VERIFIED_ATTACHMENT_EVIDENCE_REF,
    VERIFIED_ATTACHMENT_SENTINEL,
    AnswerDisposition,
    AnswerProvenance,
    FormFieldV1,
    ReasonCode,
)

DATASET = Path(__file__).parent / "fixtures" / "v4" / "form_resolution_bilingual_240.json"


def _profile() -> UserProfile:
    return UserProfile.model_validate(
        {
            "personal": {
                "name": "Test Candidate",
                "email": "candidate@example.test",
                "phone": "+10000000000",
                "location": "Test City, Test Country",
            },
            "links": {"linkedin": "https://example.test/profile"},
            "evidence": {
                "user_confirmed": {
                    "work_authorization": "yes",
                    "visa_sponsorship": "yes",
                    "citizenship": "yes",
                    "security_clearance": "yes",
                    "license": "yes",
                },
                "cv_extracted_by_artifact": {
                    "c" * 64: {
                        "primary_language": "Python",
                        "highest_degree": "BSc",
                        "python_years": "5",
                        "professional_level": "senior",
                        "engineering_domain": "software",
                    }
                },
            },
        }
    )


def _context(locale: str = "en"):
    from core.form_planning import AnswerPolicyContext

    return AnswerPolicyContext(
        profile=_profile(),
        profile_version=7,
        selected_cv_id="synthetic-cv",
        selected_cv_hash="c" * 64,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
        locale=locale,
    )


@pytest.mark.asyncio
async def test_bilingual_dataset_matches_the_bounded_resolution_policy() -> None:
    from core.form_planning import AnswerPolicyV1, LLMFieldAnswerV1
    from scripts.evaluate_v4_quality import (
        _answer_context,
        _FixtureTypedClient,
        _synthetic_profile,
    )

    rows = json.loads(DATASET.read_text(encoding="utf-8"))
    correct = 0
    resolved = 0
    abstained = 0
    typed_local_calls = 0
    sensitive_local_calls = 0
    for row in rows:
        field = FormFieldV1.model_validate(row["field"])
        llm_output = row["llm_output"]
        client = (
            _FixtureTypedClient(
                json.dumps(
                    LLMFieldAnswerV1.model_validate(llm_output).model_dump(mode="json"),
                    sort_keys=True,
                )
            )
            if llm_output is not None
            else None
        )
        result = await AnswerPolicyV1(llm_client=client).plan_fields(
            (field,),
            _answer_context(_synthetic_profile(), row["locale"]),
        )
        decision = result.decisions[0]
        expected = row["expected"]
        matches = (
            decision.provenance.value == expected["provenance"]
            and decision.disposition.value == expected["disposition"]
            and decision.value == expected["value"]
            and (decision.reason_code.value if decision.reason_code is not None else None)
            == expected["reason_code"]
            and list(decision.evidence_refs) == expected["evidence_refs"]
            and bool(client and client.provider_calls) == expected["llm_called"]
        )
        correct += int(matches)
        typed_local_calls += int(bool(client and client.provider_calls))
        if row["id"].startswith("label-sensitive-") and client:
            sensitive_local_calls += client.provider_calls
        if decision.disposition == AnswerDisposition.RESOLVED:
            resolved += 1
        else:
            abstained += 1

    assert correct == len(rows)
    assert resolved == 160
    assert abstained == 80
    assert typed_local_calls == 80
    assert sensitive_local_calls == 0


@pytest.mark.asyncio
async def test_sensitive_question_never_reaches_the_llm() -> None:
    from core.form_planning import AnswerPolicyV1

    class ExplodingClient:
        async def generate_typed(self, **_kwargs):
            raise AssertionError("sensitive data reached the LLM")

    field = FormFieldV1.model_validate(
        {
            "field_id": "nationality",
            "canonical_name": "nationality",
            "label": "מהו הלאום שלך?",
            "field_type": "text",
            "required": True,
            "position": 0,
        }
    )
    result = await AnswerPolicyV1(llm_client=ExplodingClient()).plan_fields(
        (field,),
        _context("he"),
    )

    assert result.decisions[0].disposition == AnswerDisposition.OPERATOR_REQUIRED
    assert result.decisions[0].provenance == AnswerProvenance.ABSTAINED
    assert result.decisions[0].reason_code == ReasonCode.REQUIRED_FIELD_UNKNOWN
    assert result.blockers == (ReasonCode.REQUIRED_FIELD_UNKNOWN,)


@pytest.mark.asyncio
async def test_cv_extracted_sensitive_fact_is_not_authoritative() -> None:
    from core.form_planning import AnswerPolicyV1

    profile = _profile()
    profile.evidence.user_confirmed.pop("work_authorization")
    profile.evidence.cv_extracted["work_authorization"] = "yes"
    context = replace(_context(), profile=profile)
    field = FormFieldV1.model_validate(
        {
            "field_id": "authorization",
            "canonical_name": "work_authorization",
            "label": "Are you authorized to work?",
            "field_type": "radio",
            "required": True,
            "position": 0,
            "sensitive_category": "authorization",
            "options": [
                {"value": "yes", "label": "Yes"},
                {"value": "no", "label": "No"},
            ],
        }
    )

    result = await AnswerPolicyV1().plan_fields((field,), context)
    assert result.decisions[0].disposition == AnswerDisposition.OPERATOR_REQUIRED
    assert result.decisions[0].value is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected_provenance"),
    [
        ("confirmed", AnswerProvenance.USER_CONFIRMED),
        ("cv", AnswerProvenance.CV_EVIDENCE),
    ],
)
async def test_reviewed_label_semantics_use_known_evidence_before_llm(
    source: str,
    expected_provenance: AnswerProvenance,
) -> None:
    from core.form_planning import AnswerPolicyV1

    class ExplodingClient:
        calls = 0

        async def generate_typed(self, **_kwargs):
            self.calls += 1
            raise AssertionError("known evidence must resolve before the LLM")

    profile = _profile()
    if source == "confirmed":
        profile.evidence.user_confirmed["primary_language"] = "Python"
        profile.evidence.cv_extracted_by_artifact["c" * 64].pop("primary_language")
    context = replace(_context(), profile=profile)
    field = FormFieldV1.model_validate(
        {
            "field_id": "unannotated-language",
            "canonical_name": None,
            "label": "Primary programming language",
            "field_type": "text",
            "required": True,
            "position": 0,
        }
    )
    client = ExplodingClient()

    result = await AnswerPolicyV1(llm_client=client).plan_fields((field,), context)

    decision = result.decisions[0]
    assert decision.value == "Python"
    assert decision.provenance == expected_provenance
    assert decision.evidence_refs == (
        (
            "profile:user_confirmed:primary_language"
            if source == "confirmed"
            else f"cv:{'c' * 64}:primary_language"
        ),
    )
    assert client.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "value", "label"),
    [
        ("cv", "Canadian citizen", "Additional information"),
        ("cv", "אזרחות קנדית", "מידע נוסף"),
        ("confirmed", "Gender: nonbinary", "Additional information"),
        ("confirmed", "מגדר: אחר", "מידע נוסף"),
    ],
)
async def test_benign_evidence_key_cannot_launder_sensitive_value(
    source: str,
    value: str,
    label: str,
) -> None:
    from core.form_planning import AnswerPolicyV1

    class ExplodingClient:
        calls = 0

        async def generate_typed(self, **_kwargs):
            self.calls += 1
            raise AssertionError("protected evidence must never reach the LLM")

    profile = _profile()
    if source == "cv":
        profile.evidence.cv_extracted_by_artifact["c" * 64]["misc_note"] = value
    else:
        profile.evidence.user_confirmed["misc_note"] = value
    context = replace(_context(), profile=profile)
    field = FormFieldV1.model_validate(
        {
            "field_id": "misc-note",
            "canonical_name": "misc_note",
            "label": label,
            "field_type": "text",
            "required": True,
            "position": 0,
        }
    )
    client = ExplodingClient()

    result = await AnswerPolicyV1(llm_client=client).plan_fields((field,), context)

    decision = result.decisions[0]
    assert decision.value is None
    assert decision.disposition is AnswerDisposition.OPERATOR_REQUIRED
    assert decision.reason_code is ReasonCode.UNSUPPORTED_CLAIM
    assert ReasonCode.REQUIRED_FIELD_UNKNOWN in result.blockers
    assert client.calls == 0


@pytest.mark.asyncio
async def test_planning_never_writes_the_legacy_question_cache(tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core.form_planning import AnswerPolicyV1
    from db.models import AnswerCache, Base, OperatorApprovedAnswer

    engine = create_engine(f"sqlite:///{tmp_path / 'answers.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    field = FormFieldV1.model_validate(
        {
            "field_id": "unknown",
            "canonical_name": "unknown_fact",
            "label": "Unknown optional fact",
            "field_type": "text",
            "required": False,
            "position": 0,
        }
    )

    await AnswerPolicyV1(db=db).plan_fields((field,), _context())

    assert db.query(AnswerCache).count() == 0
    assert db.query(OperatorApprovedAnswer).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_reviewed_resume_file_resolves_only_from_exact_verified_attachment() -> None:
    from core.form_planning import AnswerPolicyV1

    context = replace(
        _context(),
        attached_cv_id="synthetic-cv",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
    )
    field = FormFieldV1.model_validate(
        {
            "field_id": "resume-upload",
            "canonical_name": "resume_upload",
            "label": "Upload your resume",
            "field_type": "file",
            "required": True,
            "position": 0,
            "constraints": {"accepted_file_types": ["application/pdf"]},
        }
    )

    result = await AnswerPolicyV1().plan_fields((field,), context)

    decision = result.decisions[0]
    assert decision.disposition is AnswerDisposition.RESOLVED
    assert decision.provenance is AnswerProvenance.VERIFIED_ATTACHMENT
    assert decision.value == VERIFIED_ATTACHMENT_SENTINEL
    assert decision.evidence_refs == (VERIFIED_ATTACHMENT_EVIDENCE_REF,)
    assert context.selected_cv_id not in str(decision.model_dump())
    assert context.selected_cv_hash not in str(decision.model_dump())
    assert result.blockers == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attached_cv_id", "attached_cv_hash", "attachment_verified"),
    [
        ("synthetic-cv", "c" * 64, False),
        ("different-cv", "c" * 64, True),
        ("synthetic-cv", "d" * 64, True),
    ],
)
async def test_resume_file_attachment_mismatch_requires_operator(
    attached_cv_id,
    attached_cv_hash,
    attachment_verified,
) -> None:
    from core.form_planning import AnswerPolicyV1

    context = replace(
        _context(),
        attached_cv_id=attached_cv_id,
        attached_cv_hash=attached_cv_hash,
        attachment_verified=attachment_verified,
    )
    field = FormFieldV1(
        field_id="resume-upload",
        canonical_name="resume_upload",
        label="Resume upload",
        field_type="file",
        required=True,
        position=0,
    )

    result = await AnswerPolicyV1().plan_fields((field,), context)

    decision = result.decisions[0]
    assert decision.disposition is AnswerDisposition.OPERATOR_REQUIRED
    assert decision.reason_code is ReasonCode.ATTACHMENT_UNVERIFIED
    assert ReasonCode.ATTACHMENT_UNVERIFIED in result.blockers
    assert ReasonCode.REQUIRED_FIELD_UNKNOWN in result.blockers


@pytest.mark.asyncio
async def test_unreviewed_file_control_never_uses_attachment_or_llm() -> None:
    from core.form_planning import AnswerPolicyV1

    class ExplodingClient:
        async def generate_typed(self, **_kwargs):
            raise AssertionError("unreviewed file control reached the LLM")

    context = replace(
        _context(),
        attached_cv_id="synthetic-cv",
        attached_cv_hash="c" * 64,
        attachment_verified=True,
    )
    field = FormFieldV1(
        field_id="portfolio-file",
        canonical_name="supporting_document",
        label="Upload a supporting portfolio sample",
        field_type="file",
        required=True,
        position=0,
    )

    result = await AnswerPolicyV1(llm_client=ExplodingClient()).plan_fields(
        (field,),
        context,
    )

    assert result.decisions[0].disposition is AnswerDisposition.OPERATOR_REQUIRED
    assert result.decisions[0].reason_code is ReasonCode.UNSUPPORTED_CLAIM


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized_by", ["count", "serialized_size"])
async def test_oversized_form_observation_fails_before_any_llm_call(
    oversized_by,
) -> None:
    from core.form_planning import AnswerPolicyV1, FormPlanningBlockedError

    class ExplodingClient:
        calls = 0

        async def generate_typed(self, **_kwargs):
            self.calls += 1
            raise AssertionError("oversized form reached the LLM")

    count = 201 if oversized_by == "count" else 200
    padding = " x" * 700 if oversized_by == "serialized_size" else ""
    fields = tuple(
        FormFieldV1(
            field_id=f"field-{index}",
            label=f"Unknown field {index}{padding}",
            field_type="text",
            required=True,
            position=index,
        )
        for index in range(count)
    )
    client = ExplodingClient()

    with pytest.raises(FormPlanningBlockedError) as exc_info:
        await AnswerPolicyV1(llm_client=client).plan_fields(fields, _context())

    assert exc_info.value.reason_code is ReasonCode.FORM_CHANGED
    assert client.calls == 0
