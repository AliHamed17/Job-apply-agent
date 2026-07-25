"""LLM-powered salary negotiator brief generator."""

from __future__ import annotations

from dataclasses import dataclass, field
import structlog

from jobs.models import JobData
from llm.client import LLMClient, get_llm_client
from profile.models import UserProfile

logger = structlog.get_logger(__name__)

_SALARY_PROMPT = """\
Generate a salary benchmark estimation and negotiation script for candidate {name} applying for {job_title} at {company} in {location}.

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
  "negotiation_talking_points": ["3 key value-adds candidate brings to justify top-tier compensation"],
  "counter_offer_script": "Sample polite counter-offer email/verbal script"
}}
"""


@dataclass
class SalaryNegotiationBrief:
    currency: str = "ILS"
    estimated_percentiles: dict[str, int] = field(default_factory=dict)
    negotiation_talking_points: list[str] = field(default_factory=list)
    counter_offer_script: str = ""


async def generate_salary_brief(
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> SalaryNegotiationBrief:
    llm = client or get_llm_client()
    resume_content = (cv_text if cv_text and cv_text.strip() else profile.resume.text)[:4000]

    prompt = _SALARY_PROMPT.format(
        name=profile.personal.name,
        job_title=job.title,
        company=job.company,
        location=job.location or profile.personal.location,
        job_description=job.description[:3000],
        cv_text=resume_content,
    )

    try:
        raw = await llm.generate_json(
            prompt=prompt,
            system="You are an expert executive compensation consultant and tech salary negotiator in Israel & Global tech hubs.",
        )
        return SalaryNegotiationBrief(
            currency=raw.get("currency", "ILS"),
            estimated_percentiles=raw.get("estimated_percentiles", {"p25": 28000, "p50": 35000, "p75": 42000, "p90": 50000}),
            negotiation_talking_points=raw.get("negotiation_talking_points", []),
            counter_offer_script=raw.get("counter_offer_script", ""),
        )
    except Exception as exc:
        logger.error("salary_brief_generation_failed", error=str(exc))
        return SalaryNegotiationBrief(
            currency="ILS",
            estimated_percentiles={"p25": 28000, "p50": 35000, "p75": 42000, "p90": 50000},
            negotiation_talking_points=["Proven 75% build-to-deploy speedup", "Shipped 3 production LLM tools"],
            counter_offer_script="Thank you for the offer. Based on market compensation for Senior AI Engineers, I would like to discuss...",
        )
