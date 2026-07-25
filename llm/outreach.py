"""LLM-powered recruiter outreach generator."""

from __future__ import annotations

from dataclasses import dataclass
import structlog

from jobs.models import JobData
from llm.client import LLMClient, get_llm_client
from profile.models import UserProfile

logger = structlog.get_logger(__name__)

_OUTREACH_PROMPT = """\
Generate personalized recruiter outreach messages for candidate {name} applying for {job_title} at {company}.

## Job Description
{job_description}

## Candidate Aligned Background
{cv_text}

## Task
Return ONLY a JSON object with:
{{
  "linkedin_note": "Short LinkedIn connection request note (under 280 chars)",
  "email_subject": "Catchy professional email subject line",
  "email_body": "Personalized 2-paragraph email message expressing genuine interest and proposing a 15-min chat"
}}
"""


@dataclass
class OutreachPackage:
    linkedin_note: str
    email_subject: str
    email_body: str


async def generate_outreach(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> OutreachPackage:
    llm = client or get_llm_client()
    resume_content = (cv_text if cv_text and cv_text.strip() else profile.resume.text)[:4000]

    prompt = _OUTREACH_PROMPT.format(
        name=profile.personal.name,
        job_title=job.title,
        company=job.company,
        job_description=job.description[:3000],
        cv_text=resume_content,
    )

    try:
        raw = await llm.generate_json(
            prompt=prompt,
            system="You are an expert executive recruiter and talent acquisition consultant.",
        )
        return OutreachPackage(
            linkedin_note=raw.get("linkedin_note", f"Hi! Interested in the {job.title} role at {job.company}."),
            email_subject=raw.get("email_subject", f"Application for {job.title} - {profile.personal.name}"),
            email_body=raw.get("email_body", f"Dear Hiring Team at {job.company},\n\nI am writing to express my strong interest in the {job.title} position."),
        )
    except Exception as exc:
        logger.error("outreach_generation_failed", error=str(exc))
        return OutreachPackage(
            linkedin_note=f"Hi! Interested in the {job.title} role at {job.company}.",
            email_subject=f"Application for {job.title} - {profile.personal.name}",
            email_body=f"Dear Hiring Team at {job.company},\n\nI am writing to express my strong interest in the {job.title} position.",
        )
