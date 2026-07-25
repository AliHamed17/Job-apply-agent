"""Deterministic, auditable CV routing."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]{2,}", (value or "").lower()))


def parse_required_skills(value: str | None) -> list[str]:
    """Accept the stored JSON list while safely rejecting legacy free text."""
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


class CVDefinition(BaseModel):
    id: str
    file: str
    title_terms: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)


class RoutingOverride(BaseModel):
    cv_id: str
    title_contains: list[str] = Field(default_factory=list)
    description_contains: list[str] = Field(default_factory=list)
    seniority: list[str] = Field(default_factory=list)


class CVRoutingConfig(BaseModel):
    cvs: list[CVDefinition]
    overrides: list[RoutingOverride] = Field(default_factory=list)
    minimum_confidence: float = Field(default=0.35, ge=0, le=1)
    fallback_cv_id: str | None = None

    @model_validator(mode="after")
    def references_exist(self):
        ids = {cv.id for cv in self.cvs}
        references = [override.cv_id for override in self.overrides]
        if self.fallback_cv_id:
            references.append(self.fallback_cv_id)
        missing = sorted(set(references) - ids)
        if missing:
            raise ValueError(f"Unknown CV ids: {', '.join(missing)}")
        if len(ids) != len(self.cvs):
            raise ValueError("CV ids must be unique")
        return self


class RoutingJob(BaseModel):
    title: str = ""
    description: str = ""
    seniority: str = ""
    required_skills: list[str] = Field(default_factory=list)


class RoutingDecision(BaseModel):
    selected_cv_id: str | None
    selected_file: str | None
    confidence: float
    matched_evidence: list[str]
    fallback_reason: str | None = None
    overridden: bool = False
    alignment_score: float | None = None
    alignment_reasoning: str | None = None



def load_routing_config(path: str | Path) -> CVRoutingConfig:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return CVRoutingConfig.model_validate(payload)


def route_cv(job: RoutingJob, config: CVRoutingConfig) -> RoutingDecision:
    title = job.title.lower()
    description = job.description.lower()
    seniority = job.seniority.lower()
    for override in config.overrides:
        title_ok = not override.title_contains or any(
            term.lower() in title for term in override.title_contains
        )
        description_ok = not override.description_contains or any(
            term.lower() in description for term in override.description_contains
        )
        seniority_ok = not override.seniority or seniority in {
            value.lower() for value in override.seniority
        }
        if title_ok and description_ok and seniority_ok:
            cv = next(item for item in config.cvs if item.id == override.cv_id)
            return RoutingDecision(
                selected_cv_id=cv.id,
                selected_file=cv.file,
                confidence=1.0,
                matched_evidence=["ordered_override"],
                overridden=True,
            )

    title_tokens = _tokens(job.title)
    description_tokens = _tokens(job.description)
    required = {skill.lower() for skill in job.required_skills}
    ranked: list[tuple[float, str, CVDefinition, list[str]]] = []
    for cv in config.cvs:
        evidence: list[str] = []
        title_hits = sorted(title_tokens & _tokens(" ".join(cv.title_terms)))
        skill_hits = sorted((description_tokens | required) & _tokens(" ".join(cv.skills)))
        seniority_hit = bool(seniority and seniority in {s.lower() for s in cv.seniority})
        if title_hits:
            evidence.append("title:" + ",".join(title_hits))
        if skill_hits:
            evidence.append("skills:" + ",".join(skill_hits))
        if seniority_hit:
            evidence.append("seniority:" + seniority)
        title_score = min(len(title_hits) / max(len(_tokens(" ".join(cv.title_terms))), 1), 1)
        # Required-skill coverage is more meaningful than the fraction of a
        # CV's entire skill inventory that happened to appear in one posting.
        skill_denominator = len(required) if required else len(_tokens(" ".join(cv.skills)))
        skill_score = min(len(skill_hits) / max(skill_denominator, 1), 1)
        confidence = round(0.5 * title_score + 0.4 * skill_score + 0.1 * seniority_hit, 4)
        ranked.append((confidence, cv.id, cv, evidence))

    confidence, _, selected, evidence = max(ranked, default=(0.0, "", None, []))
    if selected and confidence >= config.minimum_confidence:
        return RoutingDecision(
            selected_cv_id=selected.id,
            selected_file=selected.file,
            confidence=confidence,
            matched_evidence=evidence,
        )
    if config.fallback_cv_id:
        fallback = next(cv for cv in config.cvs if cv.id == config.fallback_cv_id)
        return RoutingDecision(
            selected_cv_id=fallback.id,
            selected_file=fallback.file,
            confidence=confidence,
            matched_evidence=evidence,
            fallback_reason="confidence_below_threshold",
        )
    return RoutingDecision(
        selected_cv_id=None,
        selected_file=None,
        confidence=confidence,
        matched_evidence=evidence,
        fallback_reason="abstained_low_confidence",
    )


async def validate_cv_alignment(
    job: RoutingJob,
    decision: RoutingDecision,
    config: CVRoutingConfig,
    client=None,
) -> RoutingDecision:
    """LLM-based verification to ensure the selected CV genuinely matches the role."""
    if not decision.selected_cv_id:
        return decision

    from profile.cv_content_cache import get_cv_text_by_id
    from llm.client import get_llm_client
    from llm.prompts import CV_ALIGNMENT_PROMPT

    cv_text = get_cv_text_by_id(decision.selected_cv_id)
    if not cv_text:
        return decision

    llm = client or get_llm_client()

    cv_summary_lines = [f"- {cv.id}: {cv.file} (terms: {', '.join(cv.title_terms)})" for cv in config.cvs]
    available_cvs_info = "\n".join(cv_summary_lines)

    prompt = CV_ALIGNMENT_PROMPT.format(
        job_title=job.title,
        seniority=job.seniority or "unspecified",
        job_description=job.description[:3000],
        cv_id=decision.selected_cv_id,
        cv_text=cv_text[:4000],
        available_cvs_info=available_cvs_info,
    )

    try:
        result = await llm.generate_json(prompt=prompt, system="You are an expert technical recruiter matching CVs to job postings.")
        is_good_match = result.get("is_good_match", True)
        alignment_score = float(result.get("alignment_score", 1.0))
        reasoning = str(result.get("reasoning", ""))
        suggested_id = result.get("suggested_cv_id")

        decision.alignment_score = alignment_score
        decision.alignment_reasoning = reasoning

        if suggested_id and suggested_id != decision.selected_cv_id:
            better_cv = next((cv for cv in config.cvs if cv.id == suggested_id), None)
            if better_cv:
                decision.selected_cv_id = better_cv.id
                decision.selected_file = better_cv.file
                decision.matched_evidence.append(f"llm_suggested_realign:{suggested_id}")
                decision.overridden = True

        return decision
    except Exception as exc:
        import structlog
        structlog.get_logger(__name__).warning("cv_alignment_llm_failed", error=str(exc))
        return decision


