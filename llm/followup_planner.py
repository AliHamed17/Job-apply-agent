"""LLM-powered strategic application follow-up planner."""

from __future__ import annotations

from dataclasses import dataclass
import structlog

from jobs.models import JobData
from llm.client import LLMClient, get_llm_client
from profile.models import UserProfile

logger = structlog.get_logger(__name__)

_FOLLOWUP_PROMPT = """\
Generate a 3-stage strategic follow-up plan for candidate {name} applying for {job_title} at {company}.

## Job Description
{job_description}

## Candidate Background
{cv_text}

## Task
Return ONLY a JSON object with:
{{
  "stage1_day3_checkin": "Short polite LinkedIn connection check-in note",
  "stage2_day7_value_add": "2-paragraph value-add email to hiring manager referencing a company initiative or technical point",
  "stage3_day14_inquiry": "Polite final status inquiry message"
}}
"""


@dataclass
class FollowUpPlan:
    stage1_day3_checkin: str
    stage2_day7_value_add: str
    stage3_day14_inquiry: str


async def generate_followup_plan(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> FollowUpPlan:
    llm = client or get_llm_client()
    resume_content = (cv_text if cv_text and cv_text.strip() else profile.resume.text)[:4000]

    prompt = _FOLLOWUP_PROMPT.format(
        name=profile.personal.name,
        job_title=job.title,
        company=job.company,
        job_description=job.description[:3000],
        cv_text=resume_content,
    )

    try:
        raw = await llm.generate_json(
            prompt=prompt,
            system="You are an executive career coach specializing in recruiter follow-up strategies.",
        )
        return FollowUpPlan(
            stage1_day3_checkin=raw.get("stage1_day3_checkin", f"Hi! Following up on my application for the {job.title} position."),
            stage2_day7_value_add=raw.get("stage2_day7_value_add", f"Dear Hiring Team at {job.company},\n\nI wanted to share additional insights..."),
            stage3_day14_inquiry=raw.get("stage3_day14_inquiry", f"Hello, I am checking in regarding the status of the {job.title} role."),
        )
    except Exception as exc:
        logger.error("followup_plan_generation_failed", error=str(exc))
        return FollowUpPlan(
            stage1_day3_checkin=f"Hi! Following up on my application for the {job.title} position at {job.company}.",
            stage2_day7_value_add=f"Dear Hiring Team at {job.company},\n\nI wanted to share additional insights regarding my experience in Python and AI.",
            stage3_day14_inquiry=f"Hello, I am checking in regarding the status of the {job.title} role.",
        )
