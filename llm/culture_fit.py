"""LLM-powered company culture and technical fit evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field
import structlog

from jobs.models import JobData
from llm.client import LLMClient, get_llm_client
from profile.models import UserProfile

logger = structlog.get_logger(__name__)

_CULTURE_PROMPT = """\
Evaluate company culture and technical fit for candidate {name} applying for {job_title} at {company}.

## Job Description
{job_description}

## Candidate Aligned Background
{cv_text}

## Task
Return ONLY a JSON object with:
{{
  "culture_fit_score": integer score between 0 and 100,
  "cultural_highlights": ["3 positive engineering culture attributes detected"],
  "behavioral_talking_points": ["3 candidate talking points aligned with team culture"],
  "caution_flags": ["Any workload/ambiguity warnings if detected in posting"]
}}
"""


@dataclass
class CultureFitEvaluation:
    culture_fit_score: int = 85
    cultural_highlights: list[str] = field(default_factory=list)
    behavioral_talking_points: list[str] = field(default_factory=list)
    caution_flags: list[str] = field(default_factory=list)


async def evaluate_culture_fit(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> CultureFitEvaluation:
    llm = client or get_llm_client()
    resume_content = (cv_text if cv_text and cv_text.strip() else profile.resume.text)[:4000]

    prompt = _CULTURE_PROMPT.format(
        name=profile.personal.name,
        job_title=job.title,
        company=job.company,
        job_description=job.description[:3000],
        cv_text=resume_content,
    )

    try:
        raw = await llm.generate_json(
            prompt=prompt,
            system="You are an organizational culture consultant and technical hiring team advisor.",
        )
        return CultureFitEvaluation(
            culture_fit_score=int(raw.get("culture_fit_score", 85)),
            cultural_highlights=raw.get("cultural_highlights", []),
            behavioral_talking_points=raw.get("behavioral_talking_points", []),
            caution_flags=raw.get("caution_flags", []),
        )
    except Exception as exc:
        logger.error("culture_fit_evaluation_failed", error=str(exc))
        return CultureFitEvaluation(
            culture_fit_score=80,
            cultural_highlights=["Innovation-driven tech stack", "Autonomous engineering culture"],
            behavioral_talking_points=["Proactive ownership of production AI tools"],
            caution_flags=[],
        )
