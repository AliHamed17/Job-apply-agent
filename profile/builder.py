"""Build a UserProfile from a CV via the LLM."""

from __future__ import annotations

import structlog

from llm.client import LLMClient, get_llm_client
from profile.models import UserProfile

logger = structlog.get_logger(__name__)

_EXTRACTION_PROMPT = """You are extracting a structured job-seeker profile from a CV.
Return ONLY JSON with this exact shape (omit unknown fields, never invent facts):
{{
  "personal": {{"name": "", "email": "", "phone": "", "location": "", "work_authorization": ""}},
  "links": {{"linkedin": "", "github": "", "portfolio": ""}},
  "preferences": {{
     "roles": ["job titles this person should target, inferred from their experience"],
     "locations": ["cities/countries they can work in, plus 'Remote' if applicable"],
     "keywords": ["hard skills / technologies from the CV"],
     "seniority": ["one or more of: entry, mid, senior, lead, director"]
  }}
}}
Do not fabricate certifications, visas, or clearances. If a field is unknown, leave it empty.

CV TEXT:
{cv_text}
"""


async def build_profile_from_text(cv_text: str, client: LLMClient | None = None) -> UserProfile:
    """Extract a validated UserProfile from raw CV text."""
    if client is None:
        client = get_llm_client()

    raw = await client.generate_json(
        prompt=_EXTRACTION_PROMPT.format(cv_text=cv_text[:12000]),
    )

    # Merge into UserProfile defaults; store CV text as resume text.
    data: dict = {
        "personal": raw.get("personal", {}) or {},
        "links": raw.get("links", {}) or {},
        "preferences": raw.get("preferences", {}) or {},
        "resume": {"text": cv_text},
    }
    profile = UserProfile(**data)
    logger.info(
        "profile_built_from_cv",
        name=profile.personal.name,
        roles=len(profile.preferences.roles),
        keywords=len(profile.preferences.keywords),
    )
    return profile


async def build_profile_from_pdf(pdf_path: str, client: LLMClient | None = None) -> UserProfile:
    """Extract a UserProfile from a PDF CV file."""
    from profile.pdf_loader import extract_text_from_pdf  # noqa: PLC0415

    text = extract_text_from_pdf(pdf_path)
    if not text.strip():
        raise ValueError(f"No extractable text in PDF: {pdf_path}")
    profile = await build_profile_from_text(text, client=client)
    profile.resume.pdf_path = pdf_path
    return profile
