"""Application generation — uses LLM to produce tailored application materials."""

from __future__ import annotations

from dataclasses import dataclass, field
from profile.models import UserProfile

import structlog

from jobs.models import JobData
from llm.client import LLMClient, get_llm_client
from llm.prompts import (
    COVER_LETTER_PROMPT,
    QA_ANSWERS_PROMPT,
    RECRUITER_MESSAGE_PROMPT,
    SYSTEM_PROMPT,
    build_salary_guidance,
    build_system_prompt,
)

logger = structlog.get_logger(__name__)


@dataclass
class GeneratedApplication:
    """Container for all generated application materials."""

    cover_letter: str = ""
    recruiter_message: str = ""
    qa_answers: dict[str, str] = field(default_factory=dict)
    has_placeholders: bool = False
    placeholder_fields: list[str] = field(default_factory=list)


def _check_placeholders(text: str) -> list[str]:
    """Find [PLACEHOLDER: ...] markers in generated text."""
    import re
    return re.findall(r"\[PLACEHOLDER:\s*([^\]]+)\]", text)


def _load_few_shot_examples(limit: int = 5) -> list[dict]:
    """Load the most recent cover letter feedback pairs from the DB.

    Returns a list of dicts with keys "bad", "good", "note".
    Returns an empty list if the DB is unavailable or has no feedback.
    """
    try:
        from db.models import CoverLetterFeedback
        from db.session import get_session_factory

        db = get_session_factory()()
        try:
            rows = (
                db.query(CoverLetterFeedback)
                .order_by(CoverLetterFeedback.created_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {"bad": r.original_text, "good": r.corrected_text, "note": r.feedback_note}
                for r in rows
            ]
        finally:
            db.close()
    except Exception as exc:
        logger.warning("few_shot_load_failed", error=str(exc))
        return []


async def generate_cover_letter(
    job: JobData,
    profile: UserProfile,
    client: LLMClient | None = None,
    few_shot_examples: list[dict] | None = None,
    cv_text: str | None = None,
) -> str:
    """Generate a tailored cover letter for a specific job.

    Args:
        few_shot_examples: Optional list of {"bad", "good", "note"} dicts from
                           the feedback DB. Injected into the system prompt to
                           steer the LLM toward the user's preferred style.
                           If None, examples are auto-loaded from the DB.
        cv_text: Optional text of the specifically aligned CV.
    """
    if client is None:
        client = get_llm_client()

    if few_shot_examples is None:
        few_shot_examples = _load_few_shot_examples()

    system = build_system_prompt(few_shot_examples) if few_shot_examples else SYSTEM_PROMPT

    from llm.language import detect_language
    lang = detect_language(f"{job.title} {job.description}")
    if lang == "he":
        system += (
            "\nCRITICAL LANGUAGE INSTRUCTION: The job posting is in Hebrew. "
            "Write the cover letter in professional, fluent Hebrew."
        )

    resume_content = (cv_text if cv_text and cv_text.strip() else profile.resume.text)[:4000]


    raw_projects = getattr(profile, "projects", []) or []
    if isinstance(raw_projects, list) and raw_projects:
        project_spotlights = "\n".join(
            f"- {p.get('name', '')}: {p.get('impact', '')}"
            if isinstance(p, dict)
            else f"- {getattr(p, 'name', '')}: {getattr(p, 'impact', '')}"
            for p in raw_projects
        )
    else:
        project_spotlights = "None specified."

    prompt = COVER_LETTER_PROMPT.format(
        job_title=job.title,
        company=job.company,
        location=job.location,
        description=job.description[:3000],  # truncate to fit context
        name=profile.personal.name,
        user_location=profile.personal.location,
        work_authorization=profile.evidence.user_confirmed.get(
            "work authorization", "[CONFIRMED EVIDENCE REQUIRED]"
        ),
        resume_text=resume_content,
        project_spotlights=project_spotlights,
        cover_letter_style=profile.cover_letter.style,
    )


    result = await client.generate(prompt=prompt, system=system)
    logger.info(
        "cover_letter_generated",
        job=job.title,
        length=len(result),
        few_shot_count=len(few_shot_examples),
    )
    return result


async def generate_recruiter_message(
    job: JobData,
    profile: UserProfile,
    client: LLMClient | None = None,
) -> str:
    """Generate a short recruiter outreach message."""
    if client is None:
        client = get_llm_client()

    key_skills = ", ".join(profile.preferences.keywords[:10])

    prompt = RECRUITER_MESSAGE_PROMPT.format(
        job_title=job.title,
        company=job.company,
        name=profile.personal.name,
        key_skills=key_skills,
    )

    result = await client.generate(
        prompt=prompt, system=SYSTEM_PROMPT, max_tokens=500
    )
    logger.info("recruiter_message_generated", job=job.title, length=len(result))
    return result


async def generate_qa_answers(
    job: JobData,
    profile: UserProfile,
    client: LLMClient | None = None,
    cv_text: str | None = None,
) -> dict[str, str]:
    """Generate answers to common application questions."""
    if client is None:
        client = get_llm_client()

    salary = profile.preferences.salary
    resume_content = (cv_text if cv_text and cv_text.strip() else profile.resume.text)[:4000]

    prompt = QA_ANSWERS_PROMPT.format(
        job_title=job.title,
        company=job.company,
        name=profile.personal.name,
        user_location=profile.personal.location,
        work_authorization=profile.personal.work_authorization,
        resume_text=resume_content,
        salary_guidance=build_salary_guidance(salary.min, salary.max, salary.currency),
    )

    try:
        result = await client.generate_json(prompt=prompt, system=SYSTEM_PROMPT)
        logger.info("qa_answers_generated", job=job.title, keys=list(result.keys()))
        return result
    except Exception as exc:
        logger.error("qa_generation_failed", error=str(exc))
        return {"error": f"Failed to generate Q&A: {exc}"}


async def generate_full_application(
    job: JobData,
    profile: UserProfile,
    client: LLMClient | None = None,
    cv_text: str | None = None,
) -> GeneratedApplication:
    """Generate all application materials for a job.

    Returns a GeneratedApplication with cover letter, recruiter message,
    and Q&A answers. Checks for placeholders that need manual attention.

    Automatically loads few-shot correction examples from the DB to steer
    the cover letter toward the user's preferred style (feedback loop).
    """
    if client is None:
        client = get_llm_client()

    # Load once and pass into cover letter generation
    few_shot_examples = _load_few_shot_examples()

    cover_letter = await generate_cover_letter(
        job, profile, client, few_shot_examples, cv_text=cv_text
    )
    recruiter_msg = await generate_recruiter_message(job, profile, client)
    qa_answers = await generate_qa_answers(job, profile, client, cv_text=cv_text)

    # Check for placeholders
    all_text = cover_letter + " " + recruiter_msg + " " + str(qa_answers)
    placeholders = _check_placeholders(all_text)

    app = GeneratedApplication(
        cover_letter=cover_letter,
        recruiter_message=recruiter_msg,
        qa_answers=qa_answers,
        has_placeholders=len(placeholders) > 0,
        placeholder_fields=placeholders,
    )

    logger.info(
        "full_application_generated",
        job=job.title,
        company=job.company,
        has_placeholders=app.has_placeholders,
    )
    return app

