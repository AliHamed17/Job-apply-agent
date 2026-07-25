"""Skill gap analysis and resume recommendation module."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
import structlog

logger = structlog.get_logger(__name__)

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "have", "from", "your", "will", "our", "are",
    "must", "should", "team", "work", "experience", "skills", "years", "role", "candidate",
}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]{2,}", (text or "").lower()))


@dataclass
class SkillGapAnalysis:
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)


def analyze_skill_gaps(job_description: str, job_requirements: str, cv_text: str) -> SkillGapAnalysis:
    """Analyze skill coverage between job postings and candidate CV text."""
    job_tokens = _tokens(f"{job_description} {job_requirements}")
    cv_tokens = _tokens(cv_text)

    matched = sorted(job_tokens & cv_tokens)
    missing = sorted(job_tokens - cv_tokens)

    missing_tech = [m for m in missing if len(m) >= 3 and m not in STOPWORDS][:10]
    matched_tech = [m for m in matched if len(m) >= 3 and m not in STOPWORDS][:15]

    recommendations = []
    if missing_tech:
        recommendations.append(
            f"Consider adding or contextualizing these key posting terms in your CV: {', '.join(missing_tech[:5])}"
        )
    if len(matched_tech) >= 5:
        recommendations.append("Strong technical keyword overlap detected with candidate CV background.")

    return SkillGapAnalysis(
        matched_skills=matched_tech,
        missing_skills=missing_tech,
        recommendations=recommendations,
    )
