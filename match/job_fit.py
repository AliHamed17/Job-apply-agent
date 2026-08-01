"""Deterministic, evidence-bounded job fit and CV-alignment decisions."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from profile.cv_routing import CVDefinition, CVRoutingConfig
from profile.models import SelectedCVArtifact, UserProfile
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.sensitive_policy import contains_prompt_injection
from discovery.contracts import stable_digest
from jobs.models import JobData

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_REASON_PATTERN = r"^[A-Z][A-Z0-9_]{1,63}$"
FIT_POLICY_VERSION: Literal["job-fit-policy.v1"] = "job-fit-policy.v1"
FIT_ALGORITHM_VERSION: Literal["job-fit.v1"] = "job-fit.v1"
FIT_MODEL_IDENTITY: Literal["deterministic:job-fit-v1"] = "deterministic:job-fit-v1"


class FitDisposition(StrEnum):
    EXCLUDED = "excluded"
    NEEDS_REVIEW = "needs_review"
    ELIGIBLE = "eligible"


class FitEvidenceV1(BaseModel):
    """One bounded factor result; raw job or candidate text is never retained."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: Literal[
        "role",
        "skills",
        "location",
        "seniority",
        "employment",
        "experience",
        "language_authorization",
    ]
    result: Literal["matched", "partial", "unknown", "unmatched", "excluded"]
    points: float = Field(ge=0.0, le=100.0)
    maximum_points: float = Field(gt=0.0, le=100.0)
    reason_codes: tuple[str, ...] = Field(default=(), max_length=12)

    @field_validator("points", "maximum_points")
    @classmethod
    def finite_points(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("fit evidence points must be finite")
        return value

    @field_validator("reason_codes")
    @classmethod
    def bounded_reason_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_REASON_PATTERN, value) is None for value in values):
            raise ValueError("fit evidence reason codes must be stable bounded tokens")
        if len(set(values)) != len(values):
            raise ValueError("fit evidence reason codes must be unique")
        return values


class FitThresholdsV1(BaseModel):
    """Calibrated gates used only after an exact qualification is loaded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_fit_score: float = Field(default=85.0, ge=85.0, le=100.0)
    minimum_routing_confidence: float = Field(default=0.55, ge=0.0, le=1.0)
    minimum_routing_margin: float = Field(default=0.08, ge=0.0, le=1.0)

    @field_validator(
        "minimum_fit_score",
        "minimum_routing_confidence",
        "minimum_routing_margin",
    )
    @classmethod
    def finite_threshold(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("fit thresholds must be finite")
        return value


class FitQualificationV1(BaseModel):
    """Local qualification bound to exact routing config and CV bytes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["fit-qualification.v1"] = "fit-qualification.v1"
    algorithm_version: Literal["job-fit.v1"] = FIT_ALGORITHM_VERSION
    routing_config_digest: str = Field(pattern=_SHA256_PATTERN)
    cv_manifest_digest: str = Field(pattern=_SHA256_PATTERN)
    dataset_digest: str = Field(pattern=_SHA256_PATTERN)
    thresholds: FitThresholdsV1
    labeled_cases: int = Field(ge=240)
    holdout_cases: int = Field(ge=48)
    holdout_precision: float = Field(ge=0.0, le=1.0)
    holdout_coverage: float = Field(ge=0.0, le=1.0)
    qualified: bool = False
    created_at: datetime

    @field_validator("holdout_precision", "holdout_coverage")
    @classmethod
    def finite_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("qualification metrics must be finite")
        return value

    @model_validator(mode="after")
    def qualified_artifacts_meet_release_threshold(self) -> FitQualificationV1:
        if self.qualified and self.holdout_precision < 0.95:
            raise ValueError("qualified fit routing requires at least 95% holdout precision")
        return self

    @property
    def qualification_digest(self) -> str:
        return stable_digest(self.model_dump(mode="json"))


class CalibratedRoutingDecisionV1(BaseModel):
    """v5-only ranking result, kept separate from the qualified v4 LLM contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_cv_id: str | None = Field(default=None, max_length=255)
    selected_file: str | None = Field(default=None, max_length=1024)
    confidence: float = Field(ge=0.0, le=1.0)
    matched_evidence: tuple[str, ...] = Field(default=(), max_length=20)
    fallback_reason: str | None = Field(default=None, max_length=64)
    overridden: bool = False
    second_best_cv_id: str | None = Field(default=None, max_length=255)
    second_best_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    confidence_margin: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("confidence", "second_best_confidence", "confidence_margin")
    @classmethod
    def finite_confidence(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("calibrated routing metrics must be finite")
        return value


class JobFitDecisionV1(BaseModel):
    """Immutable quality decision; eligibility is not submission authority."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["job-fit-decision.v1"] = "job-fit-decision.v1"
    job_digest: str = Field(pattern=_SHA256_PATTERN)
    profile_version: int | None = Field(default=None, ge=1)
    routing_config_digest: str = Field(pattern=_SHA256_PATTERN)
    cv_manifest_digest: str = Field(pattern=_SHA256_PATTERN)
    selected_cv_id: str | None = Field(default=None, max_length=255)
    selected_cv_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    routing_confidence: float = Field(ge=0.0, le=1.0)
    routing_margin: float = Field(ge=0.0, le=1.0)
    routing_fallback_reason: str | None = Field(default=None, max_length=64)
    fit_score: float = Field(ge=0.0, le=100.0)
    disposition: FitDisposition
    quality_eligible: bool = False
    hard_exclusions: tuple[str, ...] = Field(default=(), max_length=20)
    uncertainty: tuple[str, ...] = Field(default=(), max_length=30)
    unsupported_required_skills: tuple[str, ...] = Field(default=(), max_length=100)
    evidence: tuple[FitEvidenceV1, ...] = Field(min_length=7, max_length=7)
    policy_version: Literal["job-fit-policy.v1"] = FIT_POLICY_VERSION
    model_identity: Literal["deterministic:job-fit-v1"] = FIT_MODEL_IDENTITY
    thresholds: FitThresholdsV1
    qualification_digest: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @field_validator("routing_confidence", "routing_margin", "fit_score")
    @classmethod
    def finite_decision_metric(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("fit decision metrics must be finite")
        return value

    @field_validator("hard_exclusions", "uncertainty")
    @classmethod
    def stable_reasons(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(re.fullmatch(_REASON_PATTERN, value) is None for value in values):
            raise ValueError("fit decision reasons must be stable bounded tokens")
        if tuple(dict.fromkeys(values)) != values:
            raise ValueError("fit decision reasons must be ordered and unique")
        return values

    @model_validator(mode="after")
    def eligibility_is_consistent(self) -> JobFitDecisionV1:
        if self.selected_cv_hash is not None and self.selected_cv_id is None:
            raise ValueError("selected CV hash requires a selected CV id")
        if self.quality_eligible:
            if (
                self.disposition != FitDisposition.ELIGIBLE
                or self.hard_exclusions
                or self.uncertainty
                or self.unsupported_required_skills
                or self.routing_fallback_reason is not None
                or self.selected_cv_id is None
                or self.selected_cv_hash is None
                or self.qualification_digest is None
                or self.fit_score < self.thresholds.minimum_fit_score
                or self.routing_confidence < self.thresholds.minimum_routing_confidence
                or self.routing_margin < self.thresholds.minimum_routing_margin
            ):
                raise ValueError("quality eligibility contradicts fit evidence")
        elif self.disposition == FitDisposition.ELIGIBLE:
            raise ValueError("eligible disposition requires quality_eligible=true")
        return self

    @property
    def decision_digest(self) -> str:
        return stable_digest(self.model_dump(mode="json"))


def load_fit_qualification(path: str | Path) -> FitQualificationV1:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return FitQualificationV1.model_validate(payload)


def routing_config_digest(config: CVRoutingConfig) -> str:
    return stable_digest(config.model_dump(mode="json"))


def cv_manifest_digest(artifacts: Mapping[str, SelectedCVArtifact]) -> str:
    return stable_digest(
        [
            {"cv_id": cv_id, "pdf_sha256": artifact.pdf_sha256}
            for cv_id, artifact in sorted(artifacts.items())
        ]
    )


def job_content_digest(job: JobData) -> str:
    return stable_digest(
        {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "employment_type": job.employment_type,
            "seniority": job.seniority,
            "description": job.description,
            "requirements": job.requirements,
            "keywords": job.keywords,
        }
    )


def qualification_matches(
    qualification: FitQualificationV1 | None,
    *,
    config_digest: str,
    manifest_digest: str,
) -> bool:
    return bool(
        qualification is not None
        and qualification.qualified
        and qualification.algorithm_version == FIT_ALGORITHM_VERSION
        and qualification.routing_config_digest == config_digest
        and qualification.cv_manifest_digest == manifest_digest
    )


def _normalized(value: str) -> str:
    return " ".join(re.sub(r"[^\w+#.]+", " ", value.casefold()).split())


def _contains_term(text: str, term: str) -> bool:
    normalized_text = f" {_normalized(text)} "
    normalized_term = _normalized(term)
    return bool(normalized_term and f" {normalized_term} " in normalized_text)


def _ordered_unique(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _routing_tokens(value: str) -> set[str]:
    return {
        token.strip("_")
        for token in re.findall(r"[\w+#.]+", (value or "").casefold())
        if len(token.strip("_")) >= 2
    }


def _route_cv_calibrated(
    job: JobData,
    required_skills: tuple[str, ...],
    config: CVRoutingConfig,
) -> CalibratedRoutingDecisionV1:
    title = job.title.casefold()
    description = f"{job.description} {job.requirements}".casefold()
    seniority = job.seniority.casefold()
    for override in config.overrides:
        title_ok = not override.title_contains or any(
            term.casefold() in title for term in override.title_contains
        )
        description_ok = not override.description_contains or any(
            term.casefold() in description for term in override.description_contains
        )
        seniority_ok = not override.seniority or seniority in {
            value.casefold() for value in override.seniority
        }
        if title_ok and description_ok and seniority_ok:
            cv = next(item for item in config.cvs if item.id == override.cv_id)
            return CalibratedRoutingDecisionV1(
                selected_cv_id=cv.id,
                selected_file=cv.file,
                confidence=1.0,
                matched_evidence=("ordered_override",),
                overridden=True,
                confidence_margin=1.0,
            )

    title_tokens = _routing_tokens(job.title)
    description_tokens = _routing_tokens(description)
    required_tokens = _routing_tokens(" ".join(required_skills))
    ranked: list[tuple[float, str, CVDefinition, tuple[str, ...]]] = []
    for cv in config.cvs:
        evidence: list[str] = []
        cv_title_tokens = _routing_tokens(" ".join(cv.title_terms))
        cv_skill_tokens = _routing_tokens(" ".join(cv.skills))
        title_hits = sorted(title_tokens & cv_title_tokens)
        skill_hits = sorted((description_tokens | required_tokens) & cv_skill_tokens)
        seniority_hit = bool(
            seniority and seniority in {value.casefold() for value in cv.seniority}
        )
        if title_hits:
            evidence.append("title:" + ",".join(title_hits))
        if skill_hits:
            evidence.append("skills:" + ",".join(skill_hits))
        if seniority_hit:
            evidence.append("seniority:" + seniority)
        title_score = min(len(title_hits) / max(len(cv_title_tokens), 1), 1.0)
        skill_denominator = len(required_tokens) if required_tokens else len(cv_skill_tokens)
        skill_score = min(len(skill_hits) / max(skill_denominator, 1), 1.0)
        confidence = round(
            0.5 * title_score + 0.4 * skill_score + 0.1 * seniority_hit,
            4,
        )
        ranked.append((confidence, cv.id, cv, tuple(evidence)))

    ranked.sort(key=lambda item: (-item[0], item[1].casefold()))
    confidence, _, selected, top_evidence = ranked[0] if ranked else (0.0, "", None, ())
    second_best_cv_id = ranked[1][2].id if len(ranked) > 1 else None
    second_best_confidence = ranked[1][0] if len(ranked) > 1 else 0.0
    confidence_margin = round(max(0.0, confidence - second_best_confidence), 4)
    if selected is not None and confidence >= config.minimum_confidence:
        return CalibratedRoutingDecisionV1(
            selected_cv_id=selected.id,
            selected_file=selected.file,
            confidence=confidence,
            matched_evidence=top_evidence,
            second_best_cv_id=second_best_cv_id,
            second_best_confidence=second_best_confidence,
            confidence_margin=confidence_margin,
        )
    if config.fallback_cv_id:
        fallback = next(cv for cv in config.cvs if cv.id == config.fallback_cv_id)
        return CalibratedRoutingDecisionV1(
            selected_cv_id=fallback.id,
            selected_file=fallback.file,
            fallback_reason="confidence_below_threshold",
            confidence=confidence,
            matched_evidence=top_evidence,
            second_best_cv_id=second_best_cv_id,
            second_best_confidence=second_best_confidence,
            confidence_margin=confidence_margin,
        )
    return CalibratedRoutingDecisionV1(
        fallback_reason="abstained_low_confidence",
        confidence=confidence,
        matched_evidence=top_evidence,
        second_best_cv_id=second_best_cv_id,
        second_best_confidence=second_best_confidence,
        confidence_margin=confidence_margin,
    )


_TECH_SKILL_VOCABULARY = (
    "python",
    "java",
    "javascript",
    "typescript",
    "react",
    "angular",
    "vue",
    "node.js",
    "go",
    "golang",
    "rust",
    "c",
    "c++",
    "c#",
    ".net",
    "spring",
    "django",
    "fastapi",
    "sql",
    "postgresql",
    "mysql",
    "mongodb",
    "redis",
    "kafka",
    "spark",
    "airflow",
    "pandas",
    "pytorch",
    "tensorflow",
    "scikit-learn",
    "kubernetes",
    "docker",
    "terraform",
    "ansible",
    "jenkins",
    "aws",
    "azure",
    "gcp",
    "linux",
    "selenium",
    "playwright",
    "rtos",
    "5g",
    "sctp",
    "git",
    "פייתון",
    "גאווה",
    "קוברנטיס",
    "טרפורם",
)


def _required_skills(job: JobData, config: CVRoutingConfig) -> tuple[str, ...]:
    vocabulary = _ordered_unique(
        [skill for cv in config.cvs for skill in cv.skills] + list(_TECH_SKILL_VOCABULARY)
    )
    explicit = _ordered_unique(list(job.keywords))
    explicit_known = [
        skill
        for skill in vocabulary
        if any(skill == item or _contains_term(item, skill) for item in explicit)
    ]
    source_text = job.requirements.strip() or job.description
    textual = [skill for skill in vocabulary if _contains_term(source_text, skill)]
    return _ordered_unique([*explicit_known, *textual])


def _selected_definition(
    config: CVRoutingConfig, selected_cv_id: str | None
) -> CVDefinition | None:
    return next((cv for cv in config.cvs if cv.id == selected_cv_id), None)


_ISRAEL_TERMS = (
    "israel",
    "tel aviv",
    "jerusalem",
    "haifa",
    "herzliya",
    "petah tikva",
    "beer sheva",
    "beersheba",
    "ישראל",
    "תל אביב",
    "ירושלים",
    "חיפה",
    "הרצליה",
    "פתח תקווה",
    "באר שבע",
)
_REMOTE_TERMS = ("remote", "work from home", "מרחוק", "עבודה מהבית")
_REMOTE_ALLOWED_REGIONS = (
    "worldwide",
    "anywhere",
    "global remote",
    "remote global",
    "emea",
    "middle east",
    "israel",
    "עולמי",
    "ישראל",
)
_REMOTE_INCOMPATIBLE_REGIONS = (
    "us",
    "us only",
    "usa",
    "usa only",
    "united states",
    "united states only",
    "canada",
    "canada only",
    "uk",
    "uk only",
    "eu",
    "eu only",
    "europe",
    "europe only",
    "apac",
    "apac only",
    "americas",
    "americas only",
)


def _location_factor(
    job: JobData, profile: UserProfile
) -> tuple[FitEvidenceV1, list[str], list[str]]:
    location = _normalized(job.location)
    scope_text = _normalized(f"{job.location} {job.description} {job.requirements}")
    remote_scope_text = _normalized(f"{job.location} {job.description}")
    hard: list[str] = []
    uncertain: list[str] = []
    is_remote = any(_contains_term(scope_text, term) for term in _REMOTE_TERMS)
    if is_remote:
        if any(_contains_term(remote_scope_text, term) for term in _REMOTE_INCOMPATIBLE_REGIONS):
            hard.append("REMOTE_REGION_INCOMPATIBLE")
            return (
                FitEvidenceV1(
                    factor="location",
                    result="excluded",
                    points=0,
                    maximum_points=20,
                    reason_codes=("REMOTE_REGION_INCOMPATIBLE",),
                ),
                hard,
                uncertain,
            )
        if any(_contains_term(remote_scope_text, term) for term in _REMOTE_ALLOWED_REGIONS):
            if not profile.preferences.remote_ok:
                hard.append("REMOTE_WORK_NOT_PERMITTED")
                result = "excluded"
                points = 0.0
                reasons = ("REMOTE_WORK_NOT_PERMITTED",)
            else:
                result = "matched"
                points = 20.0
                reasons = ("REMOTE_REGION_ISRAEL_COMPATIBLE",)
            return (
                FitEvidenceV1(
                    factor="location",
                    result=result,
                    points=points,
                    maximum_points=20,
                    reason_codes=reasons,
                ),
                hard,
                uncertain,
            )
        uncertain.append("REMOTE_REGION_UNSPECIFIED")
        return (
            FitEvidenceV1(
                factor="location",
                result="unknown",
                points=10,
                maximum_points=20,
                reason_codes=("REMOTE_REGION_UNSPECIFIED",),
            ),
            hard,
            uncertain,
        )

    if any(_contains_term(location, term) for term in _ISRAEL_TERMS):
        hybrid = _contains_term(scope_text, "hybrid") or _contains_term(scope_text, "היברידי")
        permitted = profile.preferences.hybrid_ok if hybrid else profile.preferences.onsite_ok
        if not permitted:
            hard.append("WORKPLACE_MODE_NOT_PERMITTED")
            result = "excluded"
            points = 0.0
            reasons = ("WORKPLACE_MODE_NOT_PERMITTED",)
        else:
            result = "matched"
            points = 20.0
            reasons = ("ISRAEL_LOCATION_MATCH",)
        return (
            FitEvidenceV1(
                factor="location",
                result=result,
                points=points,
                maximum_points=20,
                reason_codes=reasons,
            ),
            hard,
            uncertain,
        )

    if not location:
        uncertain.append("LOCATION_ELIGIBILITY_UNKNOWN")
        return (
            FitEvidenceV1(
                factor="location",
                result="unknown",
                points=10,
                maximum_points=20,
                reason_codes=("LOCATION_ELIGIBILITY_UNKNOWN",),
            ),
            hard,
            uncertain,
        )

    hard.append("FOREIGN_ONSITE_ROLE")
    return (
        FitEvidenceV1(
            factor="location",
            result="excluded",
            points=0,
            maximum_points=20,
            reason_codes=("FOREIGN_ONSITE_ROLE",),
        ),
        hard,
        uncertain,
    )


def _seniority(value: str) -> str | None:
    text = _normalized(value)
    groups = (
        ("internship", ("intern", "internship", "student", "מתמחה", "סטודנט")),
        ("junior", ("junior", "entry level", "graduate", "גוניור", "מתחיל")),
        ("lead", ("principal", "staff", "lead", "architect", "ראש צוות")),
        ("director", ("director", "head of", "vp", "מנהל")),
        ("senior", ("senior", "sr", "בכיר")),
        ("mid", ("mid", "middle", "intermediate")),
    )
    for normalized, terms in groups:
        if any(_contains_term(text, term) for term in terms):
            return normalized
    return None


def _seniority_factor(
    job: JobData, selected: CVDefinition | None
) -> tuple[FitEvidenceV1, list[str], list[str]]:
    hard: list[str] = []
    uncertain: list[str] = []
    observed = _seniority(f"{job.seniority} {job.title}")
    if observed is None:
        uncertain.append("SENIORITY_UNKNOWN")
        return (
            FitEvidenceV1(
                factor="seniority",
                result="unknown",
                points=5,
                maximum_points=10,
                reason_codes=("SENIORITY_UNKNOWN",),
            ),
            hard,
            uncertain,
        )
    supported = {
        _seniority(item) or _normalized(item) for item in (selected.seniority if selected else [])
    }
    if selected is None or observed not in supported:
        hard.append("SENIORITY_OUT_OF_SCOPE")
        return (
            FitEvidenceV1(
                factor="seniority",
                result="excluded",
                points=0,
                maximum_points=10,
                reason_codes=("SENIORITY_OUT_OF_SCOPE",),
            ),
            hard,
            uncertain,
        )
    return (
        FitEvidenceV1(
            factor="seniority",
            result="matched",
            points=10,
            maximum_points=10,
            reason_codes=("SENIORITY_MATCH",),
        ),
        hard,
        uncertain,
    )


def _employment_factor(
    job: JobData, selected: CVDefinition | None
) -> tuple[FitEvidenceV1, list[str], list[str]]:
    hard: list[str] = []
    uncertain: list[str] = []
    employment = _normalized(f"{job.employment_type} {job.title}")
    selected_scope = _normalized(
        " ".join([*(selected.title_terms if selected else []), selected.id if selected else ""])
    )
    if _contains_term(employment, "intern") or _contains_term(employment, "internship"):
        if _contains_term(selected_scope, "intern") or _contains_term(selected_scope, "student"):
            result, points, reasons = "matched", 5.0, ("INTERNSHIP_ROUTE_MATCH",)
        else:
            hard.append("EMPLOYMENT_TYPE_OUT_OF_SCOPE")
            result, points, reasons = "excluded", 0.0, ("EMPLOYMENT_TYPE_OUT_OF_SCOPE",)
    elif _contains_term(employment, "full time") or _contains_term(employment, "fulltime"):
        result, points, reasons = "matched", 5.0, ("FULL_TIME_MATCH",)
    elif not _normalized(job.employment_type):
        uncertain.append("EMPLOYMENT_TYPE_UNKNOWN")
        result, points, reasons = "unknown", 2.5, ("EMPLOYMENT_TYPE_UNKNOWN",)
    else:
        uncertain.append("EMPLOYMENT_TYPE_REVIEW_REQUIRED")
        result, points, reasons = "partial", 2.5, ("EMPLOYMENT_TYPE_REVIEW_REQUIRED",)
    return (
        FitEvidenceV1(
            factor="employment",
            result=result,
            points=points,
            maximum_points=5,
            reason_codes=reasons,
        ),
        hard,
        uncertain,
    )


def _required_years(value: str) -> int | None:
    raw = value.casefold().replace("–", "-").replace("—", "-")
    range_match = re.search(
        r"\b(\d{1,2})\s*(?:-|to|עד)\s*\d{1,2}\s*(?:years?|yrs?|שנים)\b",
        raw,
    )
    if range_match:
        return int(range_match.group(1))
    normalized = _normalized(value)
    values = [
        int(match.group(1))
        for match in re.finditer(
            r"\b(\d{1,2})\s*\+?\s*(?:years?|yrs?|שנות|שנים)\b",
            normalized,
        )
    ]
    return max(values) if values else None


def _experience_factor(
    job: JobData,
    profile: UserProfile,
    selected_cv_hash: str | None,
) -> tuple[FitEvidenceV1, list[str], list[str]]:
    hard: list[str] = []
    uncertain: list[str] = []
    required = _required_years(f"{job.requirements} {job.description}")
    if required is None:
        return (
            FitEvidenceV1(
                factor="experience",
                result="matched",
                points=3,
                maximum_points=3,
                reason_codes=("EXPERIENCE_NOT_EXPLICITLY_CONSTRAINED",),
            ),
            hard,
            uncertain,
        )
    candidate_text = profile.evidence.confirmed_fact("years_experience") or ""
    if not candidate_text and selected_cv_hash:
        facts = profile.evidence.facts_for_cv(selected_cv_hash)
        candidate_text = " ".join(
            filter(None, [facts.get("relevant_experience"), facts.get("technical_summary")])
        )
    candidate = _required_years(candidate_text)
    if candidate is None:
        uncertain.append("EXPERIENCE_EVIDENCE_MISSING")
        result, points, reasons = "unknown", 1.5, ("EXPERIENCE_EVIDENCE_MISSING",)
    elif candidate < required:
        hard.append("EXPERIENCE_REQUIREMENT_UNMET")
        result, points, reasons = "excluded", 0.0, ("EXPERIENCE_REQUIREMENT_UNMET",)
    else:
        result, points, reasons = "matched", 3.0, ("EXPERIENCE_REQUIREMENT_MET",)
    return (
        FitEvidenceV1(
            factor="experience",
            result=result,
            points=points,
            maximum_points=3,
            reason_codes=reasons,
        ),
        hard,
        uncertain,
    )


def _negative_fact(value: str) -> bool:
    normalized = _normalized(value)
    return normalized in {"no", "false", "not authorized", "none", "לא"} or normalized.startswith(
        "not "
    )


def _positive_fact(value: str) -> bool:
    normalized = _normalized(value)
    return bool(
        normalized
        and not _negative_fact(value)
        and (
            normalized in {"yes", "true", "authorized", "eligible", "כן"}
            or "israel" in normalized
            or "ישראל" in normalized
        )
    )


def _language_authorization_factor(
    job: JobData, profile: UserProfile
) -> tuple[FitEvidenceV1, list[str], list[str]]:
    hard: list[str] = []
    uncertain: list[str] = []
    text = _normalized(f"{job.requirements} {job.description}")
    confirmed = profile.evidence.user_confirmed

    language_required: str | None = None
    if any(
        phrase in text
        for phrase in (
            "fluent hebrew",
            "hebrew required",
            "native hebrew",
            "עברית חובה",
            "עברית ברמת",
        )
    ):
        language_required = "hebrew"
    elif any(
        phrase in text
        for phrase in ("fluent english", "english required", "native english", "אנגלית חובה")
    ):
        language_required = "english"
    if language_required:
        languages = _normalized(
            " ".join(
                filter(
                    None,
                    [
                        confirmed.get("languages"),
                        confirmed.get("primary_language"),
                    ],
                )
            )
        )
        aliases = ("hebrew", "עברית") if language_required == "hebrew" else ("english", "אנגלית")
        if not any(_contains_term(languages, alias) for alias in aliases):
            uncertain.append("LANGUAGE_EVIDENCE_MISSING")

    authorization_required = any(
        phrase in text
        for phrase in (
            "authorized to work",
            "work authorization",
            "right to work",
            "אישור עבודה",
        )
    )
    if authorization_required:
        authorization = str(confirmed.get("work_authorization") or "").strip()
        if not authorization:
            uncertain.append("AUTHORIZATION_EVIDENCE_MISSING")
        elif _negative_fact(authorization):
            hard.append("AUTHORIZATION_REQUIREMENT_UNMET")
        elif not _positive_fact(authorization):
            uncertain.append("AUTHORIZATION_EVIDENCE_AMBIGUOUS")

    sponsorship_prohibited = any(
        phrase in text for phrase in ("no sponsorship", "cannot sponsor", "without sponsorship")
    )
    if sponsorship_prohibited:
        sponsorship = str(confirmed.get("visa_sponsorship") or "").strip()
        if not sponsorship:
            uncertain.append("SPONSORSHIP_EVIDENCE_MISSING")
        elif _positive_fact(sponsorship):
            hard.append("SPONSORSHIP_REQUIREMENT_UNMET")
        elif not _negative_fact(sponsorship):
            uncertain.append("SPONSORSHIP_EVIDENCE_AMBIGUOUS")

    if "security clearance" in text or "סיווג בטחוני" in text:
        clearance = str(confirmed.get("security_clearance") or "").strip()
        if not clearance:
            uncertain.append("CLEARANCE_EVIDENCE_MISSING")
        elif _negative_fact(clearance):
            hard.append("CLEARANCE_REQUIREMENT_UNMET")

    reason_codes = tuple(dict.fromkeys([*hard, *uncertain]))
    if hard:
        result, points = "excluded", 0.0
    elif uncertain:
        result, points = "unknown", 1.0
    else:
        result, points = "matched", 2.0
        reason_codes = ("LANGUAGE_AUTHORIZATION_COMPATIBLE",)
    return (
        FitEvidenceV1(
            factor="language_authorization",
            result=result,
            points=points,
            maximum_points=2,
            reason_codes=reason_codes,
        ),
        hard,
        uncertain,
    )


def evaluate_job_fit(
    job: JobData,
    profile: UserProfile,
    *,
    profile_version: int | None,
    routing_config: CVRoutingConfig,
    artifacts: Mapping[str, SelectedCVArtifact],
    qualification: FitQualificationV1 | None = None,
) -> JobFitDecisionV1:
    """Evaluate one posting without LLM calls or inferred sensitive facts."""

    config_digest = routing_config_digest(routing_config)
    manifest_digest = cv_manifest_digest(artifacts)
    qualification_bound = qualification_matches(
        qualification,
        config_digest=config_digest,
        manifest_digest=manifest_digest,
    )
    thresholds = (
        qualification.thresholds
        if qualification_bound and qualification is not None
        else FitThresholdsV1()
    )
    required_skills = _required_skills(job, routing_config)
    routing = _route_cv_calibrated(job, required_skills, routing_config)
    selected = _selected_definition(routing_config, routing.selected_cv_id)
    artifact = artifacts.get(routing.selected_cv_id or "")
    selected_hash = artifact.pdf_sha256 if artifact is not None else None

    hard: list[str] = []
    uncertain: list[str] = []
    evidence: list[FitEvidenceV1] = []

    role_points = round(35.0 * routing.confidence, 4)
    role_reasons: list[str] = []
    if routing.selected_cv_id is None:
        uncertain.append("ROUTING_ABSTAINED")
        role_reasons.append("ROUTING_ABSTAINED")
    if routing.fallback_reason is not None:
        uncertain.append("ROUTING_FALLBACK_NOT_AUTOPILOT_ELIGIBLE")
        role_reasons.append("ROUTING_FALLBACK_NOT_AUTOPILOT_ELIGIBLE")
    if routing.confidence < thresholds.minimum_routing_confidence:
        uncertain.append("ROUTING_CONFIDENCE_BELOW_THRESHOLD")
        role_reasons.append("ROUTING_CONFIDENCE_BELOW_THRESHOLD")
    if routing.confidence_margin < thresholds.minimum_routing_margin:
        uncertain.append("ROUTING_MARGIN_BELOW_THRESHOLD")
        role_reasons.append("ROUTING_MARGIN_BELOW_THRESHOLD")
    if artifact is None:
        uncertain.append("CV_ARTIFACT_UNVERIFIED")
        role_reasons.append("CV_ARTIFACT_UNVERIFIED")
    role_has_blocker = bool(role_reasons)
    if not role_reasons:
        role_reasons.append("ROUTING_EVIDENCE_MATCH")
    evidence.append(
        FitEvidenceV1(
            factor="role",
            result="partial" if role_has_blocker else "matched",
            points=role_points,
            maximum_points=35,
            reason_codes=tuple(dict.fromkeys(role_reasons)),
        )
    )

    selected_skills = {_normalized(skill) for skill in (selected.skills if selected else [])}
    unsupported = tuple(skill for skill in required_skills if skill not in selected_skills)
    if not required_skills:
        skill_points = 12.5
        skill_result = "unknown"
        skill_reasons = ("REQUIRED_SKILLS_UNKNOWN",)
        uncertain.append("REQUIRED_SKILLS_UNKNOWN")
    else:
        supported_count = len(required_skills) - len(unsupported)
        skill_points = round(25.0 * supported_count / len(required_skills), 4)
        if unsupported:
            skill_result = "partial" if supported_count else "unmatched"
            skill_reasons = ("REQUIRED_SKILLS_UNSUPPORTED",)
            uncertain.append("REQUIRED_SKILLS_UNSUPPORTED")
        else:
            skill_result = "matched"
            skill_reasons = ("REQUIRED_SKILLS_SUPPORTED",)
    evidence.append(
        FitEvidenceV1(
            factor="skills",
            result=skill_result,
            points=skill_points,
            maximum_points=25,
            reason_codes=skill_reasons,
        )
    )

    factor_builders = (
        _location_factor(job, profile),
        _seniority_factor(job, selected),
        _employment_factor(job, selected),
        _experience_factor(job, profile, selected_hash),
        _language_authorization_factor(job, profile),
    )
    for factor, factor_hard, factor_uncertain in factor_builders:
        evidence.append(factor)
        hard.extend(factor_hard)
        uncertain.extend(factor_uncertain)

    if profile.preferences.blacklist_companies and any(
        blacklisted.casefold() in job.company.casefold()
        or job.company.casefold() in blacklisted.casefold()
        for blacklisted in profile.preferences.blacklist_companies
        if blacklisted.strip() and job.company.strip()
    ):
        hard.append("BLACKLISTED_COMPANY")

    untrusted_text = f"{job.title} {job.description} {job.requirements}"
    if contains_prompt_injection(untrusted_text):
        uncertain.append("PROMPT_INJECTION_DETECTED")
    if profile_version is None:
        uncertain.append("PROFILE_VERSION_MISSING")
    if not qualification_bound:
        uncertain.append("FIT_QUALIFICATION_MISSING_OR_MISMATCHED")

    hard_tuple = tuple(dict.fromkeys(hard))
    uncertain_tuple = tuple(dict.fromkeys(uncertain))
    fit_score = round(sum(item.points for item in evidence), 4)
    if fit_score < thresholds.minimum_fit_score:
        uncertain_tuple = tuple(dict.fromkeys([*uncertain_tuple, "FIT_SCORE_BELOW_THRESHOLD"]))
    if hard_tuple:
        disposition = FitDisposition.EXCLUDED
    elif uncertain_tuple or unsupported:
        disposition = FitDisposition.NEEDS_REVIEW
    else:
        disposition = FitDisposition.ELIGIBLE
    quality_eligible = disposition == FitDisposition.ELIGIBLE

    return JobFitDecisionV1(
        job_digest=job_content_digest(job),
        profile_version=profile_version,
        routing_config_digest=config_digest,
        cv_manifest_digest=manifest_digest,
        selected_cv_id=routing.selected_cv_id,
        selected_cv_hash=selected_hash,
        routing_confidence=routing.confidence,
        routing_margin=routing.confidence_margin,
        routing_fallback_reason=routing.fallback_reason,
        fit_score=fit_score,
        disposition=disposition,
        quality_eligible=quality_eligible,
        hard_exclusions=hard_tuple,
        uncertainty=uncertain_tuple,
        unsupported_required_skills=unsupported,
        evidence=tuple(evidence),
        thresholds=thresholds,
        qualification_digest=(
            qualification.qualification_digest if qualification_bound and qualification else None
        ),
    )


def unavailable_job_fit_decision(
    job: JobData,
    *,
    profile_version: int | None,
    reason_code: str,
) -> JobFitDecisionV1:
    """Create a stable review decision when configured evaluation cannot run."""

    if re.fullmatch(_REASON_PATTERN, reason_code) is None:
        reason_code = "FIT_EVALUATION_UNAVAILABLE"
    evidence = tuple(
        FitEvidenceV1(
            factor=factor,
            result="unknown",
            points=0,
            maximum_points=maximum,
            reason_codes=(reason_code,),
        )
        for factor, maximum in (
            ("role", 35.0),
            ("skills", 25.0),
            ("location", 20.0),
            ("seniority", 10.0),
            ("employment", 5.0),
            ("experience", 3.0),
            ("language_authorization", 2.0),
        )
    )
    return JobFitDecisionV1(
        job_digest=job_content_digest(job),
        profile_version=profile_version,
        routing_config_digest="0" * 64,
        cv_manifest_digest="0" * 64,
        routing_confidence=0,
        routing_margin=0,
        fit_score=0,
        disposition=FitDisposition.NEEDS_REVIEW,
        uncertainty=(reason_code, "FIT_SCORE_BELOW_THRESHOLD"),
        evidence=evidence,
        thresholds=FitThresholdsV1(),
    )
