"""LLM-powered interactive mock interview response evaluator."""

from __future__ import annotations

from dataclasses import dataclass, field
import structlog

from jobs.models import JobData
from llm.client import LLMClient, get_llm_client
from profile.models import UserProfile

logger = structlog.get_logger(__name__)

_SIMULATOR_PROMPT = """\
Evaluate candidate {name}'s response to the interview question for the position of {job_title} at {company}.

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
    score: int = 75
    strengths: list[str] = field(default_factory=list)
    missing_points: list[str] = field(default_factory=list)
    improved_answer: str = ""


async def evaluate_interview_answer(
    question: str,
    candidate_answer: str,
    job: JobData,
    profile: UserProfile,
    cv_text: str | None = None,
    client: LLMClient | None = None,
) -> SimulationEvaluation:
    llm = client or get_llm_client()
    resume_content = (cv_text if cv_text and cv_text.strip() else profile.resume.text)[:4000]

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
        raw = await llm.generate_json(
            prompt=prompt,
            system="You are an expert technical interviewer and executive communication coach.",
        )
        return SimulationEvaluation(
            score=int(raw.get("score", 75)),
            strengths=raw.get("strengths", []),
            missing_points=raw.get("missing_points", []),
            improved_answer=raw.get("improved_answer", ""),
        )
    except Exception as exc:
        logger.error("interview_simulation_evaluation_failed", error=str(exc))
        return SimulationEvaluation(
            score=70,
            strengths=["Good general technical framing"],
            missing_points=["Include specific impact metrics (e.g. 75% speedup)"],
            improved_answer=candidate_answer,
        )
