"""Build the sanitized, deterministic Job Apply Agent v4 quality baseline."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from profile.cv_routing import (  # noqa: E402
    RoutingJob,
    load_routing_config,
    route_cv,
)
from profile.cv_routing_llm import (  # noqa: E402
    CVRoutingLLMResponseV1,
    select_cv_via_llm,
)
from profile.models import UserProfile  # noqa: E402

from core.form_planning import (  # noqa: E402
    AnswerPolicyContext,
    AnswerPolicyV1,
    LLMFieldAnswerV1,
)
from core.sensitive_policy import contains_prompt_injection  # noqa: E402
from core.submission_domain import (  # noqa: E402
    AnswerDecisionV1,
    AnswerDisposition,
    AnswerProvenance,
    AnswerValue,
    FieldType,
    FormFieldV1,
    ReasonCode,
    field_is_sensitive,
)
from jobs.models import JobData  # noqa: E402
from llm.claim_evidence import (  # noqa: E402
    ClaimBlocker,
    ClaimEvaluationMetricsV1,
    ClaimEvidenceQuoteV1,
    DraftClaimV1,
    make_evidence_item,
    validate_claim_evidence,
)
from llm.client import LLMClient  # noqa: E402
from llm.contracts import (  # noqa: E402
    ModelIdentity,
    TypedGeneration,
    TypedGenerationError,
)
from llm.generation import (  # noqa: E402
    MaterialCompositionPlanV1,
    material_input_has_prompt_injection,
)

REPORT_SCHEMA_VERSION = "v4-offline-quality-baseline-v2"
DEFAULT_FIXTURES = ROOT / "tests" / "fixtures" / "v4"
DEFAULT_JSON_OUTPUT = ROOT / "docs" / "qualification" / "v4-quality-baseline.json"
DEFAULT_MARKDOWN_OUTPUT = ROOT / "docs" / "qualification" / "v4-quality-baseline.md"

HIGH_CONFIDENCE_THRESHOLD = 0.75
MINIMUM_HIGH_CONFIDENCE_CASES = 24
MAX_FIXTURE_BYTES = 1_500_000
MAX_FIXTURE_STRING_CHARS = 10_000
MAX_FIXTURE_NODES = 75_000
_CV_HASH = "c" * 64
_SYNTHETIC_EMAIL = "candidate@example.test"
_SYNTHETIC_PHONE = "+10000000000"
_SYNTHETIC_URL = "https://example.test/profile"
_UNKNOWN_EVIDENCE_PREFIX = "ev_"

_EMAIL_RE = re.compile(r"(?i)\b[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}\b")
_URL_RE = re.compile(r"(?i)\bhttps?://[^\s\"'<>]+")
_PHONE_RE = re.compile(r"(?<![\w])\+?\d(?:[\d ()-]{7,}\d)(?![\w])")

_RoutingCategory = Literal[
    "AI/ML",
    "data",
    "software",
    "QA",
    "DevOps",
    "infrastructure",
    "embedded",
    "junior",
    "internship",
    "semantic_fallback",
    "ambiguous",
    "out_of_scope",
]
_CVId = Literal["ai-ml", "data", "software", "platform"]
_BoundaryName = Literal["form", "routing", "material"]
_BoundaryResult = Literal["typed_rejected", "semantic_blocked"]
_BoundaryReason = Literal[
    "LLM_OUTPUT_INVALID",
    "UNSUPPORTED_CLAIM",
    "REQUIRED_FIELD_UNKNOWN",
    "llm_abstained",
    "llm_evidence_unverified",
    "CLAIM_EVIDENCE_UNKNOWN",
    "SENSITIVE_CLAIM_PROHIBITED",
    "UNDECLARED_FACTUAL_CLAIM",
    "PROHIBITED_GENERATED_CONTENT",
    "UNTRUSTED_INPUT_BLOCKED",
    "llm_input_rejected",
]
_ShortText = Annotated[str, Field(min_length=1, max_length=200)]
_LongText = Annotated[str, Field(max_length=5_000)]


class _FixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _RowFixtureModel(_FixtureModel):
    id: str


_RowT = TypeVar("_RowT", bound=_RowFixtureModel)


class _RoutingJobFixtureV1(_FixtureModel):
    title: str = Field(max_length=200)
    description: str = Field(max_length=3_000)
    seniority: str = Field(max_length=32)
    required_skills: tuple[Annotated[str, Field(min_length=1, max_length=80)], ...] = Field(
        max_length=20
    )


class _RoutingCaseV1(_RowFixtureModel):
    id: str = Field(pattern=r"^route-\d{3}$")
    category: _RoutingCategory
    job: _RoutingJobFixtureV1
    expected_cv_id: _CVId | None


class _FormExpectedV1(_FixtureModel):
    provenance: AnswerProvenance
    disposition: AnswerDisposition
    value: AnswerValue | None = None
    reason_code: ReasonCode | None = None
    evidence_refs: tuple[Annotated[str, Field(min_length=1, max_length=255)], ...] = Field(
        max_length=8
    )
    llm_called: bool


class _FormCaseV1(_RowFixtureModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_+-]{1,127}$")
    locale: Literal["en", "he"]
    field: FormFieldV1
    llm_output: LLMFieldAnswerV1 | None = None
    expected: _FormExpectedV1

    @model_validator(mode="after")
    def fixture_contract_is_consistent(self) -> _FormCaseV1:
        if self.field.field_id != self.id:
            raise ValueError("form fixture id must match field_id")
        if self.expected.llm_called and self.llm_output is None:
            raise ValueError("an expected typed-client call requires llm_output")
        if self.expected.disposition is AnswerDisposition.RESOLVED:
            if self.expected.value is None or not self.expected.evidence_refs:
                raise ValueError("expected resolved answers require value and evidence")
            if self.expected.reason_code is not None:
                raise ValueError("expected resolved answers cannot carry a reason")
        elif self.expected.value is not None or self.expected.evidence_refs:
            raise ValueError("expected abstentions cannot carry value or evidence")
        return self


class _ClaimEvidenceQuoteFixtureV1(_FixtureModel):
    evidence_ref: _ShortText
    quote: Annotated[str, Field(min_length=1, max_length=800)]


class _ClaimSegmentV1(_FixtureModel):
    text: str = Field(min_length=1, max_length=1_000)
    claim_text: str | None = Field(default=None, max_length=1_000)
    factual: bool
    declare_claim: bool
    evidence_quotes: tuple[_ClaimEvidenceQuoteFixtureV1, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def declared_claim_has_contract(self) -> _ClaimSegmentV1:
        if self.declare_claim and (
            not self.factual or not self.claim_text or not self.evidence_quotes
        ):
            raise ValueError("declared factual claims require text and exact evidence quotes")
        if not self.declare_claim and (self.claim_text is not None or self.evidence_quotes):
            raise ValueError("undeclared segments cannot carry claim evidence")
        return self


class _ClaimCaseV1(_RowFixtureModel):
    id: str = Field(pattern=r"^claim-[a-z0-9_-]{1,100}$")
    evidence_catalog: dict[_ShortText, Annotated[str, Field(min_length=1, max_length=800)]] = Field(
        min_length=1,
        max_length=40,
    )
    segments: tuple[_ClaimSegmentV1, ...] = Field(min_length=1, max_length=8)
    expected_eligible: bool
    expected_blockers: tuple[ClaimBlocker, ...] = Field(max_length=8)

    @model_validator(mode="after")
    def eligibility_matches_expected_blockers(self) -> _ClaimCaseV1:
        if self.expected_eligible == bool(self.expected_blockers):
            raise ValueError("claim eligibility must be the inverse of expected blockers")
        if len(self.expected_blockers) != len(set(self.expected_blockers)):
            raise ValueError("expected blockers must be unique")
        return self


class _MalformedCaseV1(_RowFixtureModel):
    id: str = Field(pattern=r"^boundary-\d{2}$")
    boundary: _BoundaryName
    output: str = Field(max_length=MAX_FIXTURE_STRING_CHARS)
    untrusted_input: str | None = Field(default=None, max_length=2_000)
    prompt_injection: bool
    expected_result: _BoundaryResult
    expected_reasons: tuple[_BoundaryReason, ...] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def injection_reaches_semantic_boundary(self) -> _MalformedCaseV1:
        if self.prompt_injection and self.expected_result != "semantic_blocked":
            raise ValueError("prompt-injection fixtures must exercise semantic policy")
        if self.prompt_injection:
            if not self.untrusted_input or not contains_prompt_injection(self.untrusted_input):
                raise ValueError(
                    "prompt-injection fixtures require detected untrusted production input"
                )
        elif self.untrusted_input is not None:
            raise ValueError("schema-only fixtures cannot carry untrusted input")
        if self.expected_result == "typed_rejected" and self.expected_reasons != (
            "LLM_OUTPUT_INVALID",
        ):
            raise ValueError("typed rejections must expect LLM_OUTPUT_INVALID")
        return self


class _FixtureTypedClient(LLMClient):
    """Deterministic local provider that still uses the production typed boundary."""

    def __init__(self, output: str) -> None:
        self.output = output
        self.provider_calls = 0

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(
            provider="fixture",
            model="offline-contract-v1",
            local=True,
            digest=f"sha256:{'d' * 64}",
        )

    async def generate(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
        temperature: float = 0.3,
    ) -> str:
        del prompt, system, max_tokens, temperature
        self.provider_calls += 1
        return self.output

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        max_tokens: int = 2000,
    ) -> dict[str, Any]:
        del prompt, system, max_tokens
        self.provider_calls += 1
        return cast(dict[str, Any], json.loads(self.output))


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _assert_synthetic_contact_data(value: str, *, path: Path) -> None:
    for email in _EMAIL_RE.findall(value):
        if email.casefold() != _SYNTHETIC_EMAIL:
            raise ValueError(f"{path.name} contains a non-synthetic email address")
    for url in _URL_RE.findall(value):
        if url.rstrip(".,);") != _SYNTHETIC_URL:
            raise ValueError(f"{path.name} contains a non-synthetic URL")
    for phone in _PHONE_RE.findall(value):
        normalized = re.sub(r"[ ()-]", "", phone)
        if normalized != _SYNTHETIC_PHONE:
            raise ValueError(f"{path.name} contains a non-synthetic phone number")


def _validate_fixture_tree(payload: object, *, path: Path) -> None:
    stack: list[tuple[object, int]] = [(payload, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_FIXTURE_NODES:
            raise ValueError(f"{path.name} contains too many values")
        if depth > 12:
            raise ValueError(f"{path.name} exceeds the nesting bound")
        if isinstance(value, str):
            if len(value) > MAX_FIXTURE_STRING_CHARS:
                raise ValueError(f"{path.name} contains an oversized string")
            _assert_synthetic_contact_data(value, path=path)
        elif isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str) or not key or len(key) > 200:
                    raise ValueError(f"{path.name} contains an invalid object key")
                _assert_synthetic_contact_data(key, path=path)
                stack.append((item, depth + 1))
        elif isinstance(value, (list, tuple)):
            stack.extend((item, depth + 1) for item in value)
        elif value is not None and not isinstance(value, (bool, int, float)):
            raise ValueError(f"{path.name} contains an unsupported JSON value")


def _read_bounded_text(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_FIXTURE_BYTES:
        raise ValueError(f"{path.name} exceeds the fixture byte bound")
    return path.read_text(encoding="utf-8")


def _read_rows(
    path: Path,
    expected_count: int,
    row_model: type[_RowT],
) -> list[_RowT]:
    payload = json.loads(_read_bounded_text(path))
    _validate_fixture_tree(payload, path=path)
    if not isinstance(payload, list) or len(payload) != expected_count:
        raise ValueError(f"{path.name} must contain exactly {expected_count} rows")
    rows = [row_model.model_validate(row) for row in payload]
    if len({row.id for row in rows}) != len(rows):
        raise ValueError(f"{path.name} contains duplicate ids")
    return rows


def _validate_routing_config(path: Path) -> None:
    text = _read_bounded_text(path)
    _validate_fixture_tree(text, path=path)
    config = load_routing_config(path)
    if {cv.id for cv in config.cvs} != {"ai-ml", "data", "software", "platform"}:
        raise ValueError("routing fixture config must contain only sanitized CV ids")
    if any(
        not cv.file.startswith("synthetic-") or not cv.file.endswith(".pdf") for cv in config.cvs
    ):
        raise ValueError("routing fixture config must use synthetic filenames")


def _dataset_record(path: Path, count: int) -> dict[str, Any]:
    return {
        "file": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "cases": count,
    }


_SYNTHETIC_CONFIRMED = {
    "work_authorization": "yes",
    "work_permit": "yes",
    "right_to_work": "yes",
    "visa_sponsorship": "no",
    "sponsorship": "no",
    "nationality": "Syntheticland",
    "citizenship": "Syntheticland",
    "security_clearance": "no",
    "clearance": "no",
    "license": "yes",
    "licensing": "yes",
    "certification": "Synthetic Certificate",
    "gender": "prefer_not_to_say",
    "race": "prefer_not_to_say",
    "ethnicity": "prefer_not_to_say",
    "disability": "prefer_not_to_say",
    "veteran_status": "no",
    "marital_status": "prefer_not_to_say",
    "religion": "prefer_not_to_say",
    "age": "30",
}

_SYNTHETIC_CV_FACTS = {
    "primary_language": "Developed production services in Python",
    "backend_framework": "Implemented backend APIs with FastAPI",
    "database_skill": "Designed auditable schemas in PostgreSQL",
    "cloud_platform": "Deployed services on AWS",
    "container_platform": "Operated workloads on Kubernetes",
    "iac_tool": "Authored infrastructure modules with Terraform",
    "data_tool": "Built distributed data pipelines with Spark",
    "ml_framework": "Developed machine learning models with PyTorch",
    "frontend_language": "Built user interfaces in TypeScript",
    "frontend_framework": "Implemented frontend components with React",
    "test_framework": "Wrote automated tests with Pytest",
    "automation_tool": "Automated browser tests with Selenium",
    "operating_system": "Operated production services on Linux",
    "embedded_language": "Developed embedded software in C++",
    "realtime_system": "Built real-time software with FreeRTOS",
    "analytics_tool": "Created analytics dashboards with Tableau",
    "pipeline_tool": "Built data transformations with dbt",
    "api_style": "Designed REST APIs",
    "version_control": "Managed source code with Git",
    "highest_degree": "Completed a BSc degree",
}


def _synthetic_profile() -> UserProfile:
    return UserProfile.model_validate(
        {
            "personal": {
                "name": "Test Candidate",
                "email": _SYNTHETIC_EMAIL,
                "phone": _SYNTHETIC_PHONE,
                "location": "Test City, Test Country",
            },
            "links": {"linkedin": _SYNTHETIC_URL},
            "evidence": {
                "user_confirmed": _SYNTHETIC_CONFIRMED,
                "cv_extracted_by_artifact": {_CV_HASH: _SYNTHETIC_CV_FACTS},
            },
        }
    )


def _answer_context(profile: UserProfile, locale: str) -> AnswerPolicyContext:
    return AnswerPolicyContext(
        profile=profile,
        profile_version=7,
        selected_cv_id="synthetic-cv",
        selected_cv_hash=_CV_HASH,
        adapter_name="fixture",
        adapter_version="1.0.0",
        selector_version="fixture-v2",
        form_fingerprint="f" * 64,
        locale=locale,
        attached_cv_id="synthetic-cv",
        attached_cv_hash=_CV_HASH,
        attachment_verified=True,
    )


def _evaluate_routing(
    rows: list[_RoutingCaseV1],
    config_path: Path,
) -> dict[str, Any]:
    _validate_routing_config(config_path)
    config = load_routing_config(config_path)
    correct_selected = 0
    incorrect_selected = 0
    wrong_cv_for_expected_selected = 0
    selected_when_abstain_expected = 0
    correct_abstained = 0
    missed_selection = 0
    high_confidence_correct = 0
    high_confidence_incorrect = 0
    category_counts: Counter[str] = Counter()
    expected_selected = 0
    expected_abstained = 0

    for row in rows:
        decision = route_cv(RoutingJob.model_validate(row.job.model_dump()), config)
        category_counts[row.category] += 1
        if row.expected_cv_id is None:
            expected_abstained += 1
        else:
            expected_selected += 1

        if decision.selected_cv_id is None:
            if row.expected_cv_id is None:
                correct_abstained += 1
            else:
                missed_selection += 1
            continue

        selected_correctly = decision.selected_cv_id == row.expected_cv_id
        if selected_correctly:
            correct_selected += 1
        else:
            incorrect_selected += 1
            if row.expected_cv_id is None:
                selected_when_abstain_expected += 1
            else:
                wrong_cv_for_expected_selected += 1
        if decision.confidence >= HIGH_CONFIDENCE_THRESHOLD:
            if selected_correctly:
                high_confidence_correct += 1
            else:
                high_confidence_incorrect += 1

    resolved = correct_selected + incorrect_selected
    abstained = correct_abstained + missed_selection
    high_confidence_resolved = high_confidence_correct + high_confidence_incorrect
    high_confidence_precision = _ratio(high_confidence_correct, high_confidence_resolved)
    high_confidence_coverage = _ratio(high_confidence_resolved, len(rows))
    threshold_passed = (
        high_confidence_precision >= 0.95
        and high_confidence_resolved >= MINIMUM_HIGH_CONFIDENCE_CASES
    )
    return {
        "cases": len(rows),
        "positive_class": "correct selected CV among actual high-confidence predictions",
        "precision": _ratio(correct_selected, resolved),
        "high_confidence_threshold": HIGH_CONFIDENCE_THRESHOLD,
        "high_confidence_precision": high_confidence_precision,
        "high_confidence_coverage": high_confidence_coverage,
        "overall_exact_accuracy": _ratio(correct_selected + correct_abstained, len(rows)),
        "coverage": _ratio(resolved, len(rows)),
        "abstention_rate": _ratio(abstained, len(rows)),
        "confusion_counts": {
            "expected_selected": {
                "correct_selected": correct_selected,
                "incorrect_selected": wrong_cv_for_expected_selected,
                "abstained": missed_selection,
            },
            "expected_abstained": {
                "abstained": correct_abstained,
                "selected": selected_when_abstain_expected,
            },
            "high_confidence": {
                "correct": high_confidence_correct,
                "incorrect": high_confidence_incorrect,
            },
        },
        "expected_outcome_counts": {
            "selected": expected_selected,
            "abstained": expected_abstained,
        },
        "category_counts": {
            category: category_counts.get(category, 0)
            for category in (
                "AI/ML",
                "data",
                "software",
                "QA",
                "DevOps",
                "infrastructure",
                "embedded",
                "junior",
                "internship",
                "semantic_fallback",
                "ambiguous",
                "out_of_scope",
            )
        },
        "threshold": {
            "requirement": (
                f"precision >= 0.95 for confidence >= {HIGH_CONFIDENCE_THRESHOLD:.2f} "
                f"with at least {MINIMUM_HIGH_CONFIDENCE_CASES} predictions"
            ),
            "actual": {
                "precision": high_confidence_precision,
                "predictions": high_confidence_resolved,
                "coverage": high_confidence_coverage,
            },
            "passed": threshold_passed,
        },
    }


def _decision_matches_expected(
    decision: AnswerDecisionV1,
    expected: _FormExpectedV1,
) -> bool:
    return (
        decision.provenance is expected.provenance
        and decision.disposition is expected.disposition
        and decision.value == expected.value
        and decision.reason_code is expected.reason_code
        and decision.evidence_refs == expected.evidence_refs
    )


async def _evaluate_form_resolution(rows: list[_FormCaseV1]) -> dict[str, Any]:
    profile = _synthetic_profile()
    exact_correct = 0
    resolved = 0
    resolved_correct = 0
    abstained = 0
    non_sensitive_resolved = 0
    non_sensitive_correct = 0
    sensitive_automatic_eligible = 0
    sensitive_llm_calls = 0
    unsupported_eligible = 0
    typed_llm_expected = 0
    typed_llm_exercised = 0
    typed_llm_provider_calls = 0
    provenance_counts: Counter[str] = Counter()
    confusion: dict[str, Counter[str]] = {
        "resolved": Counter(),
        "abstained": Counter(),
        "operator_required": Counter(),
    }

    for row in rows:
        output = (
            json.dumps(row.llm_output.model_dump(mode="json"), sort_keys=True)
            if row.llm_output is not None
            else None
        )
        client = _FixtureTypedClient(output) if output is not None else None
        planner = AnswerPolicyV1(llm_client=cast(Any, client))
        result = await planner.plan_fields(
            (row.field,),
            _answer_context(profile, row.locale),
        )
        decision = result.decisions[0]
        llm_called = bool(client and client.provider_calls)
        matches = _decision_matches_expected(decision, row.expected) and (
            llm_called == row.expected.llm_called
        )
        exact_correct += int(matches)
        provenance_counts[decision.provenance.value] += 1
        confusion[row.expected.disposition.value][decision.disposition.value] += 1

        is_sensitive = field_is_sensitive(row.field)
        if client:
            typed_llm_provider_calls += client.provider_calls
            if is_sensitive:
                sensitive_llm_calls += client.provider_calls
        if row.expected.llm_called:
            typed_llm_expected += 1
            typed_llm_exercised += int(llm_called)

        if decision.disposition is AnswerDisposition.RESOLVED:
            resolved += 1
            resolved_correct += int(matches)
            unsupported_eligible += int(not matches)
            if not is_sensitive:
                non_sensitive_resolved += 1
                non_sensitive_correct += int(matches)
        else:
            abstained += 1

        if (
            is_sensitive
            and decision.disposition is AnswerDisposition.RESOLVED
            and decision.provenance
            not in {
                AnswerProvenance.USER_CONFIRMED,
                AnswerProvenance.OPERATOR_APPROVED_REUSABLE,
            }
        ):
            sensitive_automatic_eligible += 1

    non_sensitive_precision = _ratio(non_sensitive_correct, non_sensitive_resolved)
    contract_mismatches = len(rows) - exact_correct
    safety_violations = sensitive_automatic_eligible + unsupported_eligible + sensitive_llm_calls
    threshold_passed = (
        non_sensitive_precision >= 0.95
        and safety_violations == 0
        and contract_mismatches == 0
        and typed_llm_exercised == typed_llm_expected
        and typed_llm_expected >= 80
    )
    return {
        "cases": len(rows),
        "positive_class": "exactly correct resolved answer with expected provenance and evidence",
        "precision": _ratio(resolved_correct, resolved),
        "non_sensitive_precision": non_sensitive_precision,
        "overall_exact_accuracy": _ratio(exact_correct, len(rows)),
        "coverage": _ratio(resolved, len(rows)),
        "abstention_rate": _ratio(abstained, len(rows)),
        "confusion_counts": {
            expected: dict(sorted(predictions.items()))
            for expected, predictions in confusion.items()
        },
        "provenance_counts": dict(sorted(provenance_counts.items())),
        "contract_mismatches": contract_mismatches,
        "typed_local_cases": typed_llm_expected,
        "typed_local_cases_exercised": typed_llm_exercised,
        "typed_local_provider_calls": typed_llm_provider_calls,
        "sensitive_llm_calls": sensitive_llm_calls,
        "sensitive_automatic_eligible": sensitive_automatic_eligible,
        "unsupported_eligible": unsupported_eligible,
        "threshold": {
            "requirement": (
                "non-sensitive precision >= 0.95, all expected decisions and typed-local "
                "cases exercised, and zero unsupported or sensitive eligibility"
            ),
            "actual": {
                "non_sensitive_precision": non_sensitive_precision,
                "contract_mismatches": contract_mismatches,
                "typed_local_cases": typed_llm_exercised,
                "safety_violations": safety_violations,
            },
            "passed": threshold_passed,
        },
    }


def _unknown_evidence_id(reference: str) -> str:
    return _UNKNOWN_EVIDENCE_PREFIX + hashlib.sha256(reference.encode()).hexdigest()[:24]


def _evaluate_claims(rows: list[_ClaimCaseV1]) -> dict[str, Any]:
    true_eligible = 0
    false_eligible = 0
    true_blocked = 0
    false_blocked = 0
    blocker_mismatches = 0
    blocker_counts: Counter[str] = Counter()

    for row in rows:
        catalog = []
        evidence_ids: dict[str, str] = {}
        for reference, text in sorted(row.evidence_catalog.items()):
            source_kind: Literal["cv", "user_confirmed"] = (
                "user_confirmed" if reference.startswith("profile:") else "cv"
            )
            item = make_evidence_item(source_kind, reference, text)
            catalog.append(item)
            evidence_ids[reference] = item.evidence_id

        material_texts: list[str] = []
        claims: list[DraftClaimV1] = []
        for index, segment in enumerate(row.segments, start=1):
            material_texts.append(segment.text)
            if not segment.declare_claim:
                continue
            assert segment.claim_text is not None
            claims.append(
                DraftClaimV1(
                    claim_id=f"claim_{index}",
                    claim_text=segment.claim_text,
                    evidence_quotes=tuple(
                        ClaimEvidenceQuoteV1(
                            evidence_id=evidence_ids.get(
                                binding.evidence_ref,
                                _unknown_evidence_id(binding.evidence_ref),
                            ),
                            quote=binding.quote,
                        )
                        for binding in segment.evidence_quotes
                    ),
                )
            )

        validation = validate_claim_evidence(material_texts, claims, catalog)
        predicted_eligible = validation.eligible
        expected_blockers = tuple(sorted(row.expected_blockers))
        actual_blockers = tuple(sorted(validation.blockers))
        blockers_match = actual_blockers == expected_blockers
        blocker_mismatches += int(not blockers_match)
        for blocker in validation.blockers:
            blocker_counts[blocker] += 1

        if row.expected_eligible and predicted_eligible and blockers_match:
            true_eligible += 1
        elif not row.expected_eligible and predicted_eligible:
            false_eligible += 1
        elif not row.expected_eligible and not predicted_eligible and blockers_match:
            true_blocked += 1
        else:
            false_blocked += 1

    predicted_eligible_count = true_eligible + false_eligible
    predicted_blocked_count = true_blocked + false_blocked
    metrics = ClaimEvaluationMetricsV1(
        total=len(rows),
        true_eligible=true_eligible,
        true_blocked=true_blocked,
        false_eligible=false_eligible,
        false_blocked=false_blocked,
        precision=_ratio(true_eligible, predicted_eligible_count),
        recall=_ratio(true_eligible, true_eligible + false_blocked),
        coverage=_ratio(predicted_eligible_count, len(rows)),
        abstention_rate=_ratio(predicted_blocked_count, len(rows)),
    )
    threshold_passed = false_eligible == 0 and blocker_mismatches == 0
    return {
        "cases": len(rows),
        "positive_class": (
            "eligible claim package with literal evidence spans, conservative "
            "rendering, and exact blocker semantics"
        ),
        "precision": metrics.precision,
        "recall": metrics.recall,
        "coverage": metrics.coverage,
        "abstention_rate": metrics.abstention_rate,
        "confusion_counts": {
            "true_eligible": true_eligible,
            "false_eligible": false_eligible,
            "true_blocked": true_blocked,
            "false_blocked": false_blocked,
        },
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "blocker_mismatches": blocker_mismatches,
        "unsupported_or_sensitive_eligible": false_eligible,
        "threshold": {
            "requirement": "zero unsafe eligibility and exact expected blocker sets",
            "actual": {
                "unsafe_eligible": false_eligible,
                "blocker_mismatches": blocker_mismatches,
            },
            "passed": threshold_passed,
        },
    }


def _boundary_model(boundary: _BoundaryName) -> type[BaseModel]:
    return cast(
        type[BaseModel],
        {
            "form": LLMFieldAnswerV1,
            "routing": CVRoutingLLMResponseV1,
            "material": MaterialCompositionPlanV1,
        }[boundary],
    )


async def _typed_boundary(
    row: _MalformedCaseV1,
) -> TypedGeneration[BaseModel]:
    client = _FixtureTypedClient(row.output)
    return await client.generate_typed(
        response_model=_boundary_model(row.boundary),
        prompt="sanitized offline production-boundary fixture",
        purpose="quality_evaluation",
        prompt_version=f"{row.boundary}-boundary-v1",
        deadline=datetime.now(UTC) + timedelta(seconds=5),
        data_classification="internal",
        max_tokens=1_000,
        temperature=0.0,
    )


async def _semantic_form_result(row: _MalformedCaseV1) -> tuple[bool, tuple[str, ...]]:
    client = _FixtureTypedClient(row.output)
    field = FormFieldV1(
        field_id=row.id,
        canonical_name=None,
        label=row.untrusted_input or "Primary programming language",
        field_type=FieldType.TEXT,
        required=True,
        position=0,
    )
    result = await AnswerPolicyV1(llm_client=cast(Any, client)).plan_fields(
        (field,),
        _answer_context(_synthetic_profile(), "en"),
    )
    decision = result.decisions[0]
    eligible = decision.disposition is AnswerDisposition.RESOLVED
    reasons = (decision.reason_code.value,) if decision.reason_code else ()
    return eligible, reasons


async def _semantic_routing_result(
    row: _MalformedCaseV1,
    config_path: Path,
) -> tuple[bool, tuple[str, ...]]:
    config = load_routing_config(config_path)
    excerpts = {
        "ai-ml": "Python PyTorch machine learning models",
        "data": "SQL dbt Spark data analytics",
        "software": "Java TypeScript React API development",
        "platform": "Kubernetes Terraform Linux firmware",
    }
    decision = await select_cv_via_llm(
        RoutingJob(
            title="Machine Learning Engineer",
            description=row.untrusted_input or "Build Python PyTorch models",
            seniority="mid",
            required_skills=["python", "pytorch"],
        ),
        config,
        excerpts,
        client=_FixtureTypedClient(row.output),
    )
    eligible = decision.selected_cv_id is not None and decision.fallback_reason is None
    reasons = (decision.fallback_reason,) if decision.fallback_reason else ()
    return eligible, reasons


def _semantic_material_result(
    row: _MalformedCaseV1,
    generated: TypedGeneration[BaseModel],
) -> tuple[bool, tuple[str, ...]]:
    if material_input_has_prompt_injection(
        JobData(
            title="Synthetic Engineer",
            company="Synthetic Company",
            description=row.untrusted_input or "",
        )
    ):
        return False, ("UNTRUSTED_INPUT_BLOCKED",)
    MaterialCompositionPlanV1.model_validate(generated.value.model_dump(mode="python"))
    # The malformed boundary has no authenticated evidence catalog. A typed
    # ordinal plan therefore remains non-renderable and must fail closed.
    return False, ("MATERIAL_COMPOSITION_INVALID",)


def _bounded_reason(reason: str) -> str:
    allowed = {
        "LLM_OUTPUT_INVALID",
        "UNSUPPORTED_CLAIM",
        "REQUIRED_FIELD_UNKNOWN",
        "llm_abstained",
        "llm_evidence_unverified",
        "CLAIM_EVIDENCE_UNKNOWN",
        "SENSITIVE_CLAIM_PROHIBITED",
        "UNDECLARED_FACTUAL_CLAIM",
        "PROHIBITED_GENERATED_CONTENT",
        "UNTRUSTED_INPUT_BLOCKED",
        "MATERIAL_COMPOSITION_INVALID",
        "llm_input_rejected",
    }
    return reason if reason in allowed else "OTHER_BOUNDED_REASON"


async def _evaluate_malformed(
    rows: list[_MalformedCaseV1],
    config_path: Path,
) -> dict[str, Any]:
    correctly_blocked = 0
    missed = 0
    typed_rejected = 0
    semantic_blocked = 0
    semantic_injection_cases = 0
    semantic_injections_blocked = 0
    reason_counts: Counter[str] = Counter()
    boundary_counts: Counter[str] = Counter()

    for row in rows:
        boundary_counts[row.boundary] += 1
        actual_result = "incorrectly_eligible"
        actual_reasons: tuple[str, ...] = ()
        generated: TypedGeneration[BaseModel] | None = None
        try:
            generated = await _typed_boundary(row)
        except TypedGenerationError as exc:
            actual_result = "typed_rejected"
            actual_reasons = (exc.reason_code.value,)
            typed_rejected += 1
        else:
            if row.expected_result == "semantic_blocked":
                if row.boundary == "form":
                    eligible, actual_reasons = await _semantic_form_result(row)
                elif row.boundary == "routing":
                    eligible, actual_reasons = await _semantic_routing_result(row, config_path)
                else:
                    eligible, actual_reasons = _semantic_material_result(row, generated)
                actual_result = "semantic_eligible" if eligible else "semantic_blocked"
                semantic_blocked += int(not eligible)
            else:
                actual_result = "typed_accepted"

        if row.prompt_injection:
            semantic_injection_cases += 1
            semantic_injections_blocked += int(actual_result == "semantic_blocked")

        bounded_reasons = tuple(_bounded_reason(reason) for reason in actual_reasons)
        for reason in bounded_reasons:
            reason_counts[reason] += 1
        matches = actual_result == row.expected_result and bounded_reasons == tuple(
            row.expected_reasons
        )
        correctly_blocked += int(matches)
        missed += int(not matches)

    threshold_passed = (
        correctly_blocked == len(rows)
        and missed == 0
        and semantic_injection_cases > 0
        and semantic_injections_blocked == semantic_injection_cases
    )
    return {
        "cases": len(rows),
        "positive_class": "invalid output or semantically unsafe output blocked",
        "precision": _ratio(correctly_blocked, len(rows)),
        "coverage": _ratio(missed, len(rows)),
        "abstention_rate": _ratio(correctly_blocked, len(rows)),
        "confusion_counts": {
            "correctly_blocked": correctly_blocked,
            "incorrectly_accepted_or_misclassified": missed,
            "typed_rejected": typed_rejected,
            "semantic_blocked": semantic_blocked,
        },
        "boundary_counts": {
            boundary: boundary_counts.get(boundary, 0)
            for boundary in ("form", "routing", "material")
        },
        "reason_counts": dict(sorted(reason_counts.items())),
        "semantic_prompt_injection_cases": semantic_injection_cases,
        "semantic_prompt_injections_blocked": semantic_injections_blocked,
        "eligible_for_preparation": missed,
        "threshold": {
            "requirement": (
                "all 30 actual production-schema cases fail closed with exact reasons, "
                "and every prompt-injection case reaches and fails semantic eligibility"
            ),
            "actual": {
                "blocked": correctly_blocked,
                "eligible_or_misclassified": missed,
                "semantic_injections_blocked": semantic_injections_blocked,
                "semantic_injection_cases": semantic_injection_cases,
            },
            "passed": threshold_passed,
        },
    }


async def evaluate_quality(fixtures_dir: Path = DEFAULT_FIXTURES) -> dict[str, Any]:
    """Evaluate all four sanitized datasets without network or model inference."""

    routing_path = fixtures_dir / "cv_routing_120.json"
    routing_config = fixtures_dir / "cv_routing_eval_config.yaml"
    forms_path = fixtures_dir / "form_resolution_bilingual_240.json"
    claims_path = fixtures_dir / "cover_letter_claims_40.json"
    malformed_path = fixtures_dir / "malformed_prompt_injection_30.json"
    routing_rows = _read_rows(routing_path, 120, _RoutingCaseV1)
    form_rows = _read_rows(forms_path, 240, _FormCaseV1)
    claim_rows = _read_rows(claims_path, 40, _ClaimCaseV1)
    malformed_rows = _read_rows(malformed_path, 30, _MalformedCaseV1)
    _validate_routing_config(routing_config)

    tasks = {
        "cv_routing": _evaluate_routing(routing_rows, routing_config),
        "form_resolution": await _evaluate_form_resolution(form_rows),
        "claim_evidence": _evaluate_claims(claim_rows),
        "malformed_output": await _evaluate_malformed(malformed_rows, routing_config),
    }
    dataset_counts = {
        "cv_routing": len(routing_rows),
        "form_resolution": len(form_rows),
        "claim_evidence": len(claim_rows),
        "malformed_output": len(malformed_rows),
    }
    thresholds = {
        "dataset_case_counts": {
            "requirement": "exact case counts are 120, 240, 40, and 30",
            "actual": dataset_counts,
            "passed": dataset_counts
            == {
                "cv_routing": 120,
                "form_resolution": 240,
                "claim_evidence": 40,
                "malformed_output": 30,
            },
        },
        "routing_high_confidence_precision": tasks["cv_routing"]["threshold"],
        "form_non_sensitive_precision": tasks["form_resolution"]["threshold"],
        "form_unsafe_eligibility": {
            "requirement": (
                "zero unsupported answers, automatic sensitive answers, or sensitive LLM calls"
            ),
            "actual": {
                "sensitive": tasks["form_resolution"]["sensitive_automatic_eligible"],
                "unsupported": tasks["form_resolution"]["unsupported_eligible"],
                "sensitive_llm_calls": tasks["form_resolution"]["sensitive_llm_calls"],
            },
            "passed": (
                tasks["form_resolution"]["sensitive_automatic_eligible"] == 0
                and tasks["form_resolution"]["unsupported_eligible"] == 0
                and tasks["form_resolution"]["sensitive_llm_calls"] == 0
            ),
        },
        "claim_unsafe_eligibility": tasks["claim_evidence"]["threshold"],
        "malformed_fail_closed": tasks["malformed_output"]["threshold"],
    }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "evaluation_mode": {
            "offline": True,
            "deterministic": True,
            "network_calls": 0,
            "ollama_calls": 0,
            "typed_fixture_provider": True,
            "private_data_used": False,
        },
        "datasets": {
            "cv_routing": _dataset_record(routing_path, len(routing_rows)),
            "form_resolution": _dataset_record(forms_path, len(form_rows)),
            "claim_evidence": _dataset_record(claims_path, len(claim_rows)),
            "malformed_output": _dataset_record(malformed_path, len(malformed_rows)),
        },
        "tasks": tasks,
        "thresholds": thresholds,
        "overall_pass": all(bool(gate["passed"]) for gate in thresholds.values()),
        "interpretation": (
            "Sanitized generated-fixture contract baseline only. This report makes no "
            "improvement, independent-label, Ollama-accuracy, live-submission, or "
            "real-world generalization claim."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render a stable aggregate-only Markdown qualification baseline."""

    tasks = report["tasks"]
    lines = [
        "# Job Apply Agent v4 Offline Quality Baseline",
        "",
        report["interpretation"],
        "",
        "## Summary",
        "",
        "| Task | Cases | Precision | Coverage | Abstention | Fixture gate |",
        "|---|---:|---:|---:|---:|:---:|",
    ]
    for label, key in (
        ("CV routing", "cv_routing"),
        ("Form resolution", "form_resolution"),
        ("Claim evidence", "claim_evidence"),
        ("Production output boundary", "malformed_output"),
    ):
        task = tasks[key]
        lines.append(
            f"| {label} | {task['cases']} | {task['precision']:.2%} | "
            f"{task['coverage']:.2%} | {task['abstention_rate']:.2%} | "
            f"{'PASS' if task['threshold']['passed'] else 'FAIL'} |"
        )

    lines.extend(
        [
            "",
            f"Overall offline fixture gate: **{'PASS' if report['overall_pass'] else 'FAIL'}**.",
            "",
            "## Thresholds",
            "",
            "| Task | Requirement | Result |",
            "|---|---|:---:|",
        ]
    )
    for key in sorted(report["thresholds"]):
        gate = report["thresholds"][key]
        requirement = str(gate["requirement"]).replace("|", "\\|")
        lines.append(
            f"| {key.replace('_', ' ').title()} | {requirement} | "
            f"{'PASS' if gate['passed'] else 'FAIL'} |"
        )

    lines.extend(
        [
            "",
            "## Confusion counts",
            "",
            f"- CV routing: {json.dumps(tasks['cv_routing']['confusion_counts'], sort_keys=True)}",
            (
                "- Form resolution: "
                f"{json.dumps(tasks['form_resolution']['confusion_counts'], sort_keys=True)}"
            ),
            (
                "- Claim evidence: "
                f"{json.dumps(tasks['claim_evidence']['confusion_counts'], sort_keys=True)}"
            ),
            (
                "- Production output boundary: "
                f"{json.dumps(tasks['malformed_output']['confusion_counts'], sort_keys=True)}"
            ),
            "",
            "## Safety observations",
            "",
            (
                "- Actual routing high-confidence cutoff: "
                f"{tasks['cv_routing']['high_confidence_threshold']:.2f}; "
                f"coverage: {tasks['cv_routing']['high_confidence_coverage']:.2%}."
            ),
            (
                "- Typed local form cases exercised: "
                f"{tasks['form_resolution']['typed_local_cases_exercised']} of "
                f"{tasks['form_resolution']['typed_local_cases']}."
            ),
            (
                "- Sensitive-field typed-provider calls: "
                f"{tasks['form_resolution']['sensitive_llm_calls']}."
            ),
            (
                "- Unsupported resolved form answers eligible: "
                f"{tasks['form_resolution']['unsupported_eligible']}."
            ),
            (f"- Claim blocker-set mismatches: {tasks['claim_evidence']['blocker_mismatches']}."),
            (
                "- Semantic prompt-injection cases blocked: "
                f"{tasks['malformed_output']['semantic_prompt_injections_blocked']} of "
                f"{tasks['malformed_output']['semantic_prompt_injection_cases']}."
            ),
            "",
            "## Method and limitations",
            "",
            (
                "- The evaluator calls the production deterministic CV router, form-answer "
                "policy, claim-to-evidence validator, and actual form, routing, and material "
                "typed response schemas."
            ),
            (
                "- Claim fixtures bind each rendered factual clause to a literal span in "
                "one authorized evidence item; denials, uncertainty, other subjects, "
                "sensitive facts, and unsupported additions must abstain."
            ),
            (
                "- A deterministic local fixture provider exercises schema validation and "
                "semantic eligibility without making network or Ollama calls."
            ),
            (
                "- English and Hebrew label-only sensitive controls are evaluated without "
                "canonical-name hints; non-sensitive synthesis uses bounded evidence citations."
            ),
            (
                "- Fixtures are generated, synthetic, and co-designed with these contracts. "
                "Their rows are not independent labels and the percentages are not an "
                "estimate of production accuracy."
            ),
            (
                "- Coverage is the fraction allowed to proceed; abstention is the fraction "
                "withheld. For production-boundary fixtures, safe rejection is the positive class."
            ),
            (
                "- A passing fixture gate does not prove behavior on changed employer forms, "
                "unseen jobs, a real Ollama model, live ATS pages, or private candidate data."
            ),
            "",
            "## Dataset integrity",
            "",
            "| Dataset | Cases | SHA-256 |",
            "|---|---:|---|",
        ]
    )
    for key in sorted(report["datasets"]):
        dataset = report["datasets"][key]
        lines.append(f"| {dataset['file']} | {dataset['cases']} | `{dataset['sha256']}` |")
    lines.append("")
    return "\n".join(lines)


def write_report(
    report: dict[str, Any],
    *,
    json_output: Path,
    markdown_output: Path,
) -> None:
    json_output.parent.mkdir(parents=True, exist_ok=True)
    markdown_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_output.write_text(render_markdown(report), encoding="utf-8")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the sanitized v4 offline quality fixtures.",
    )
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MARKDOWN_OUTPUT)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero when any offline fixture threshold fails.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = asyncio.run(evaluate_quality(args.fixtures))
    write_report(
        report,
        json_output=args.json_output,
        markdown_output=args.markdown_output,
    )
    print(
        json.dumps(
            {
                "overall_pass": report["overall_pass"],
                "report": args.json_output.name,
            },
            sort_keys=True,
        )
    )
    return int(args.check and not report["overall_pass"])


if __name__ == "__main__":
    raise SystemExit(main())
