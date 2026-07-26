"""LLM-powered salary negotiator brief generator."""

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

_SALARY_PROMPT = """\
Generate a salary benchmark estimation and negotiation script for candidate {name}
applying for {job_title} at {company} in {location}.

## Job Description
{job_description}

## Candidate Aligned Background
{cv_text}

## Task
Return ONLY a JSON object with:
{{
  "currency": "ILS or USD",
  "estimated_percentiles": {{
    "p25": 25th percentile salary integer,
    "p50": median salary integer,
    "p75": 75th percentile salary integer,
    "p90": 90th percentile salary integer
  }},
  "negotiation_talking_points": ["3 evidence-based value-adds"],
  "counter_offer_script": "Sample polite counter-offer email/verbal script"
}}
"""


@dataclass
class SalaryNegotiationBrief:
    currency: str = "ILS"
    estimated_percentiles: dict[str, int] = field(default_factory=dict)
    negotiation_talking_points: list[str] = field(default_factory=list)
    counter_offer_script: str = ""


class _SalaryPercentiles(BaseModel):
    model_config = ConfigDict(extra="forbid")

    p25: int = Field(ge=0)
    p50: int = Field(ge=0)
    p75: int = Field(ge=0)
    p90: int = Field(ge=0)


class _SalaryDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = Field(min_length=1, max_length=16)
    estimated_percentiles: _SalaryPercentiles
    negotiation_talking_points: list[str] = Field(max_length=10)
    counter_offer_script: str = Field(max_length=3000)


async def generate_salary_brief(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> SalaryNegotiationBrief:
    llm = client or get_llm_client()
    resume_source = cv_text if cv_text and cv_text.strip() else profile.resume.text
    resume_content = non_sensitive_cv_excerpt(resume_source, max_chars=4000)

    prompt = _SALARY_PROMPT.format(
        name=profile.personal.name,
        job_title=job.title,
        company=job.company,
        location=job.location or profile.personal.location,
        job_description=job.description[:3000],
        cv_text=resume_content,
    )

    try:
        require_private_candidate_context(resume_content)
        generated = await generate_private_application_typed(
            client=llm,
            response_model=_SalaryDraft,
            prompt=prompt,
            purpose=GenerationPurpose.SALARY,
            prompt_version="salary-v1",
            system=(
                "You are an expert executive compensation consultant "
                "and technology salary negotiator."
            ),
        )
        raw = generated.value
        return SalaryNegotiationBrief(
            currency=raw.currency,
            estimated_percentiles=raw.estimated_percentiles.model_dump(),
            negotiation_talking_points=raw.negotiation_talking_points,
            counter_offer_script=raw.counter_offer_script,
        )
    except Exception as exc:
        logger.error(
            "salary_brief_generation_failed",
            reason_code=bounded_private_generation_reason(exc),
        )
        return SalaryNegotiationBrief(
            currency="ILS",
            estimated_percentiles={},
            negotiation_talking_points=[],
            counter_offer_script=(
                "Automated salary guidance is unavailable; use "
                "operator-confirmed market data before negotiating."
            ),
        )
