"""Classify + extract job info from text-only WhatsApp posts."""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from llm.client import LLMClient, get_llm_client

logger = structlog.get_logger(__name__)

_KEYWORDS = ("hiring", "vacancy", "vacancies", "send cv", "send resume", "looking for",
             "we are recruiting", "job opening", "apply", "position",
             "مطلوب", "توظيف", "وظيفة", "شاغر")  # Arabic: required / hiring / job / vacancy


def looks_like_job(text: str) -> bool:
    low = (text or "").lower()
    return any(k in low for k in _KEYWORDS)


@dataclass
class ParsedPost:
    is_job: bool = False
    title: str = ""
    company: str = ""
    description: str = ""
    contact_phone: str = ""
    contact_email: str = ""


_PROMPT = """Decide if this WhatsApp message is a job posting. If yes, extract fields.
Return ONLY JSON: {{"is_job": bool, "title": "", "company": "", "description": "",
"contact_phone": "", "contact_email": ""}}. Use "" for anything absent. Do not invent contacts.

MESSAGE:
{text}
"""


async def parse_text_post(text: str, client: LLMClient | None = None) -> ParsedPost:
    if not looks_like_job(text):
        return ParsedPost(is_job=False)
    client = client or get_llm_client()
    try:
        raw = await client.generate_json(prompt=_PROMPT.format(text=text[:2000]))
    except Exception as exc:
        logger.warning("text_post_parse_failed", error=str(exc))
        return ParsedPost(is_job=False)
    return ParsedPost(
        is_job=bool(raw.get("is_job")),
        title=raw.get("title", "") or "",
        company=raw.get("company", "") or "",
        description=raw.get("description", "") or "",
        contact_phone=raw.get("contact_phone", "") or "",
        contact_email=raw.get("contact_email", "") or "",
    )
