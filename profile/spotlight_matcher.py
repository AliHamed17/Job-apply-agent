"""Evidence-bounded CV spotlight matching."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from profile.models import UserProfile

from jobs.models import JobData

_TECH_TERMS = (
    "AI",
    "machine learning",
    "LLM",
    "RAG",
    "PyTorch",
    "LangChain",
    "Python",
    "Java",
    "C++",
    "Docker",
    "Kubernetes",
    "AWS",
    "Azure",
    "CI/CD",
    "Jenkins",
    "PyTest",
    "Robot Framework",
    "QA",
    "embedded",
    "infrastructure",
)


@dataclass
class ProjectSpotlightMatch:
    spotlight_title: str
    relevant_keywords: list[str] = field(default_factory=list)
    showcase_text: str = ""


def _contains(text: str, term: str) -> bool:
    optional_plural = "s?" if term.isalpha() and len(term) >= 3 and not term.endswith("s") else ""
    pattern = rf"(?<![a-z0-9]){re.escape(term.casefold())}{optional_plural}(?![a-z0-9])"
    return re.search(pattern, text.casefold()) is not None


def match_portfolio_spotlight(
    job: JobData,
    profile: UserProfile,
) -> ProjectSpotlightMatch:
    """Return only role terms that appear in both the job and CV evidence."""
    job_text = " ".join(part for part in (job.title, job.description, job.requirements) if part)
    cv_text = " ".join(
        [
            profile.resume.text,
            *profile.evidence.cv_extracted.keys(),
            *profile.evidence.cv_extracted.values(),
        ]
    )
    overlap = [
        term for term in _TECH_TERMS if _contains(job_text, term) and _contains(cv_text, term)
    ]
    if not overlap:
        return ProjectSpotlightMatch(
            spotlight_title="No verified CV spotlight",
            showcase_text=(
                "No CV-backed project claim was generated for this role. "
                "Review the selected CV before adding a spotlight."
            ),
        )

    shown = ", ".join(overlap[:8])
    return ProjectSpotlightMatch(
        spotlight_title=f"Verified CV overlap: {shown}",
        relevant_keywords=overlap,
        showcase_text=(
            f"The selected CV explicitly contains these role-relevant terms: {shown}. "
            "No additional achievement or metric is inferred."
        ),
    )
