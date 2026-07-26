"""LLM-powered interview prep brief generator."""

from __future__ import annotations

from dataclasses import dataclass, field
from profile.models import UserProfile

import structlog
from pydantic import BaseModel, ConfigDict, Field

from jobs.models import JobData
from llm.claim_evidence import non_sensitive_cv_excerpt
from llm.client import LLMClient, get_llm_client
from llm.contracts import GenerationPurpose
from llm.private_generation import (
    bounded_private_generation_reason,
    generate_private_application_typed,
    require_private_candidate_context,
)

logger = structlog.get_logger(__name__)

_INTERVIEW_PREP_PROMPT = """\
Generate a technical interview preparation brief for candidate {name}
applying for {job_title} at {company}.

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


class _InterviewPrepDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predicted_questions: list[str] = Field(max_length=10)
    star_story_talking_points: list[str] = Field(max_length=10)
    interviewer_questions: list[str] = Field(max_length=10)


async def generate_interview_prep(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> InterviewPrepBrief:
    llm = client or get_llm_client()
    resume_source = cv_text if cv_text and cv_text.strip() else profile.resume.text
    resume_content = non_sensitive_cv_excerpt(resume_source, max_chars=4000)

    prompt = _INTERVIEW_PREP_PROMPT.format(
        name=profile.personal.name,
        job_title=job.title,
        company=job.company,
        job_description=job.description[:3000],
        cv_text=resume_content,
    )

    try:
        require_private_candidate_context(resume_content)
        generated = await generate_private_application_typed(
            client=llm,
            response_model=_InterviewPrepDraft,
            prompt=prompt,
            purpose=GenerationPurpose.INTERVIEW_PREP,
            prompt_version="interview-prep-v1",
            system="You are an expert technical interviewer and executive career coach.",
        )
        raw = generated.value
        return InterviewPrepBrief(
            predicted_questions=raw.predicted_questions,
            star_story_talking_points=raw.star_story_talking_points,
            interviewer_questions=raw.interviewer_questions,
        )
    except Exception as exc:
        logger.error(
            "interview_prep_generation_failed",
            reason_code=bounded_private_generation_reason(exc),
        )
        return InterviewPrepBrief(
            predicted_questions=["Describe your experience relevant to this role."],
            star_story_talking_points=[],
            interviewer_questions=["What are the immediate priorities for this role?"],
        )
