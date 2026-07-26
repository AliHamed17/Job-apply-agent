"""LLM-powered strategic application follow-up planner."""

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

_FOLLOWUP_PROMPT = """\
Generate a 3-stage strategic follow-up plan for candidate {name}
applying for {job_title} at {company}.

## Job Description
{job_description}

## Candidate Background
{cv_text}

## Task
Return ONLY a JSON object with:
{{
  "stage1_day3_checkin": "Short polite LinkedIn connection check-in note",
  "stage2_day7_value_add": "2-paragraph value-add email to the hiring manager",
  "stage3_day14_inquiry": "Polite final status inquiry message"
}}
"""


@dataclass
class FollowUpPlan:
    stage1_day3_checkin: str
    stage2_day7_value_add: str
    stage3_day14_inquiry: str


class _FollowUpDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage1_day3_checkin: str = Field(min_length=1, max_length=1000)
    stage2_day7_value_add: str = Field(min_length=1, max_length=3000)
    stage3_day14_inquiry: str = Field(min_length=1, max_length=1000)


async def generate_followup_plan(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> FollowUpPlan:
    llm = client or get_llm_client()
    resume_source = cv_text if cv_text and cv_text.strip() else profile.resume.text
    resume_content = non_sensitive_cv_excerpt(resume_source, max_chars=4000)

    prompt = _FOLLOWUP_PROMPT.format(
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
            response_model=_FollowUpDraft,
            prompt=prompt,
            purpose=GenerationPurpose.FOLLOWUP,
            prompt_version="followup-v1",
            system=(
                "You are an executive career coach specializing in recruiter follow-up strategies."
            ),
        )
        raw = generated.value
        return FollowUpPlan(
            stage1_day3_checkin=raw.stage1_day3_checkin,
            stage2_day7_value_add=raw.stage2_day7_value_add,
            stage3_day14_inquiry=raw.stage3_day14_inquiry,
        )
    except Exception as exc:
        logger.error(
            "followup_plan_generation_failed",
            reason_code=bounded_private_generation_reason(exc),
        )
        return FollowUpPlan(
            stage1_day3_checkin=(
                f"Hi! Following up on my application for the {job.title} position at {job.company}."
            ),
            stage2_day7_value_add=(
                f"Dear Hiring Team at {job.company},\n\n"
                f"I remain interested in the {job.title} position and would "
                "be glad to provide any additional information."
            ),
            stage3_day14_inquiry=(
                f"Hello, I am checking in regarding the status of the {job.title} role."
            ),
        )
