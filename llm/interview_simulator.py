"""LLM-powered interactive mock interview response evaluator."""

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

_SIMULATOR_PROMPT = """\
Evaluate candidate {name}'s response to the interview question for the
position of {job_title} at {company}.

## Job Description
{job_description}

## Candidate Aligned Background
{cv_text}

## Question Asked
{question}

## Candidate's Answer
{candidate_answer}

## Task
Evaluate the candidate's answer and return ONLY a JSON object with:
{{
  "score": integer score between 0 and 100,
  "strengths": ["List of strong points in the answer"],
  "missing_points": ["List of critical technical or STAR points omitted"],
  "improved_answer": "Model reframed answer incorporating candidate's real experience and metrics"
}}
"""


@dataclass
class SimulationEvaluation:
    score: int = 0
    strengths: list[str] = field(default_factory=list)
    missing_points: list[str] = field(default_factory=list)
    improved_answer: str = ""


class _SimulationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: int = Field(ge=0, le=100)
    strengths: list[str] = Field(max_length=10)
    missing_points: list[str] = Field(max_length=10)
    improved_answer: str = Field(max_length=5000)


async def evaluate_interview_answer(
    question: str,
    candidate_answer: str,
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> SimulationEvaluation:
    llm = client or get_llm_client()
    resume_source = cv_text if cv_text and cv_text.strip() else profile.resume.text
    resume_content = non_sensitive_cv_excerpt(resume_source, max_chars=4000)

    prompt = _SIMULATOR_PROMPT.format(
        name=profile.personal.name,
        job_title=job.title,
        company=job.company,
        job_description=job.description[:3000],
        cv_text=resume_content,
        question=question,
        candidate_answer=candidate_answer,
    )

    try:
        require_private_candidate_context(resume_content)
        generated = await generate_private_application_typed(
            client=llm,
            response_model=_SimulationDraft,
            prompt=prompt,
            purpose=GenerationPurpose.INTERVIEW_SIMULATION,
            prompt_version="interview-simulation-v1",
            system="You are an expert technical interviewer and executive communication coach.",
        )
        raw = generated.value
        return SimulationEvaluation(
            score=raw.score,
            strengths=raw.strengths,
            missing_points=raw.missing_points,
            improved_answer=raw.improved_answer,
        )
    except Exception as exc:
        logger.error(
            "interview_simulation_evaluation_failed",
            reason_code=bounded_private_generation_reason(exc),
        )
        return SimulationEvaluation(
            score=0,
            strengths=[],
            missing_points=["Automated interview evaluation unavailable."],
            improved_answer=candidate_answer,
        )
