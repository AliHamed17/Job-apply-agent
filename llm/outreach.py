"""LLM-powered recruiter outreach generator."""

from __future__ import annotations

from dataclasses import dataclass
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

_OUTREACH_PROMPT = """\
Generate personalized recruiter outreach messages for candidate {name}
applying for {job_title} at {company}.

## Job Description
{job_description}

## Candidate Aligned Background
{cv_text}

## Task
Return ONLY a JSON object with:
{{
  "linkedin_note": "Short LinkedIn connection request note (under 280 chars)",
  "email_subject": "Catchy professional email subject line",
  "email_body": "Personalized 2-paragraph email expressing genuine interest"
}}
"""


@dataclass
class OutreachPackage:
    linkedin_note: str
    email_subject: str
    email_body: str


class _OutreachDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    linkedin_note: str = Field(min_length=1, max_length=280)
    email_subject: str = Field(min_length=1, max_length=300)
    email_body: str = Field(min_length=1, max_length=4000)


async def generate_outreach(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> OutreachPackage:
    llm = client or get_llm_client()
    resume_source = cv_text if cv_text and cv_text.strip() else profile.resume.text
    resume_content = non_sensitive_cv_excerpt(resume_source, max_chars=4000)

    prompt = _OUTREACH_PROMPT.format(
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
            response_model=_OutreachDraft,
            prompt=prompt,
            purpose=GenerationPurpose.OUTREACH,
            prompt_version="outreach-v1",
            system="You are an expert executive recruiter and talent acquisition consultant.",
        )
        raw = generated.value
        return OutreachPackage(
            linkedin_note=raw.linkedin_note,
            email_subject=raw.email_subject,
            email_body=raw.email_body,
        )
    except Exception as exc:
        logger.error(
            "outreach_generation_failed",
            reason_code=bounded_private_generation_reason(exc),
        )
        return OutreachPackage(
            linkedin_note=f"Hi! Interested in the {job.title} role at {job.company}.",
            email_subject=f"Application for {job.title} - {profile.personal.name}",
            email_body=(
                f"Dear Hiring Team at {job.company},\n\n"
                f"I am writing to express my strong interest in the {job.title} position."
            ),
        )
