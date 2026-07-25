"""LLM-powered interview prep brief generator."""

from __future__ import annotations

from dataclasses import dataclass, field
import structlog

from jobs.models import JobData
from llm.client import LLMClient, get_llm_client
from profile.models import UserProfile

logger = structlog.get_logger(__name__)

_INTERVIEW_PREP_PROMPT = """\
Generate a technical interview preparation brief for candidate {name} applying for {job_title} at {company}.

## Job Description
{job_description}

## Candidate Aligned CV & Background
{cv_text}

## Task
Return ONLY a JSON object with:
{{
  "predicted_questions": ["5 technical interview questions specific to this role"],
  "star_story_talking_points": ["3 STAR stories based on candidate's real experience/projects"],
  "interviewer_questions": ["3 smart questions for the candidate to ask the interviewer"]
}}
"""


@dataclass
class InterviewPrepBrief:
    predicted_questions: list[str] = field(default_factory=list)
    star_story_talking_points: list[str] = field(default_factory=list)
    interviewer_questions: list[str] = field(default_factory=list)


async def generate_interview_prep(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> InterviewPrepBrief:
    llm = client or get_llm_client()
    resume_content = (cv_text if cv_text and cv_text.strip() else profile.resume.text)[:4000]

    prompt = _INTERVIEW_PREP_PROMPT.format(
        name=profile.personal.name,
        job_title=job.title,
        company=job.company,
        job_description=job.description[:3000],
        cv_text=resume_content,
    )

    try:
        raw = await llm.generate_json(
            prompt=prompt,
            system="You are an expert technical interviewer and executive career coach.",
        )
        return InterviewPrepBrief(
            predicted_questions=raw.get("predicted_questions", []),
            star_story_talking_points=raw.get("star_story_talking_points", []),
            interviewer_questions=raw.get("interviewer_questions", []),
        )
    except Exception as exc:
        logger.error("interview_prep_generation_failed", error=str(exc))
        return InterviewPrepBrief(
            predicted_questions=["Describe your experience relevant to this role."],
            star_story_talking_points=["Highlight 75% build-to-deploy speedup using Jenkins/Groovy"],
            interviewer_questions=["What are the immediate priorities for this role?"],
        )
