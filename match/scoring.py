"""Job matching and scoring engine.

Scores each job against the user's profile and preferences.
Returns a 0–100 score with a detailed breakdown and action decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import structlog

from jobs.models import JobData
from profile.models import UserProfile

logger = structlog.get_logger(__name__)


class Action(str, Enum):
    SKIP = "skip"
    DRAFT = "draft"
    AUTO_APPLY = "auto_apply"


# ── Score weights (must sum to 100) ──────────────────────
TITLE_WEIGHT = 25
KEYWORD_WEIGHT = 25
LOCATION_WEIGHT = 20
SENIORITY_WEIGHT = 15
EMPLOYMENT_WEIGHT = 10
SALARY_WEIGHT = 5  # bonus, can push above 100

SKIP_THRESHOLD = 20
AUTO_APPLY_THRESHOLD = 80


@dataclass
class ScoreBreakdown:
    """Detailed scoring breakdown with per-factor scores."""

    title_score: float = 0.0
    keyword_score: float = 0.0
    location_score: float = 0.0
    seniority_score: float = 0.0
    employment_score: float = 0.0
    salary_bonus: float = 0.0
    blacklist_penalty: float = 0.0
    total: float = 0.0
    action: Action = Action.DRAFT
    skip_reason: str | None = None

    def compute_total(self) -> float:
        self.total = max(0.0, min(100.0,
            self.title_score
            + self.keyword_score
            + self.location_score
            + self.seniority_score
            + self.employment_score
            + self.salary_bonus
            - self.blacklist_penalty
        ))
        return self.total


def _tokenize(text: str) -> set[str]:
    """Lowercase tokenization for matching."""
    return set(re.findall(r"[a-z0-9#+.]+", text.lower()))


def _score_title(job: JobData, profile: UserProfile) -> float:
    """Score based on how well the job title matches target roles (0–25)."""
    if not profile.preferences.roles:
        return TITLE_WEIGHT * 0.5  # neutral if no preference

    title_lower = job.title.lower()
    best_match = 0.0
    for role in profile.preferences.roles:
        role_lower = role.lower()
        # Exact match
        if role_lower in title_lower or title_lower in role_lower:
            best_match = 1.0
            break
        # Partial word overlap
        role_words = _tokenize(role_lower)
        title_words = _tokenize(title_lower)
        if role_words and title_words:
            overlap = len(role_words & title_words) / len(role_words)
            best_match = max(best_match, overlap)

    return TITLE_WEIGHT * best_match


def _score_keywords(job: JobData, profile: UserProfile) -> float:
    """Score based on keyword overlap between job and profile (0–25)."""
    if not profile.preferences.keywords:
        return KEYWORD_WEIGHT * 0.5

    job_text = f"{job.title} {job.description} {job.requirements}".lower()
    job_tokens = _tokenize(job_text)
    profile_keywords = profile.keyword_set

    if not profile_keywords:
        return KEYWORD_WEIGHT * 0.5

    matches = len(profile_keywords & job_tokens)
    ratio = min(1.0, matches / max(1, len(profile_keywords)))
    return KEYWORD_WEIGHT * ratio


def _score_location(job: JobData, profile: UserProfile) -> float:
    """Score based on location match (0–20)."""
    if not profile.preferences.locations:
        return LOCATION_WEIGHT * 0.5

    job_loc = job.location.lower()

    # Remote check
    if "remote" in job_loc:
        if profile.preferences.remote_ok:
            return LOCATION_WEIGHT
        return LOCATION_WEIGHT * 0.3

    # Location match
    for pref_loc in profile.preferences.locations:
        if pref_loc.lower() == "remote":
            continue
        if pref_loc.lower() in job_loc or job_loc in pref_loc.lower():
            return LOCATION_WEIGHT

    # No match found
    return 0.0


def _score_seniority(job: JobData, profile: UserProfile) -> float:
    """Score based on seniority level match (0–15)."""
    if not profile.preferences.seniority or not job.seniority:
        return SENIORITY_WEIGHT * 0.5  # neutral

    if job.seniority.lower() in [s.lower() for s in profile.preferences.seniority]:
        return SENIORITY_WEIGHT

    return SENIORITY_WEIGHT * 0.2


def _score_employment(job: JobData, profile: UserProfile) -> float:
    """Score based on employment type (0–10)."""
    if not job.employment_type:
        return EMPLOYMENT_WEIGHT * 0.5

    emp = job.employment_type.lower()
    if "full-time" in emp or "full time" in emp:
        return EMPLOYMENT_WEIGHT  # most common preference
    if "contract" in emp:
        return EMPLOYMENT_WEIGHT * 0.5
    if "internship" in emp:
        return EMPLOYMENT_WEIGHT * 0.3

    return EMPLOYMENT_WEIGHT * 0.5


def _check_blacklist(job: JobData, profile: UserProfile) -> str | None:
    """Check if job's company is blacklisted. Returns reason or None."""
    if not profile.preferences.blacklist_companies:
        return None

    company_lower = job.company.lower()
    for blacklisted in profile.preferences.blacklist_companies:
        if blacklisted.lower() in company_lower or company_lower in blacklisted.lower():
            return f"Company '{job.company}' is blacklisted"

    return None


def score_job(job: JobData, profile: UserProfile) -> ScoreBreakdown:
    """Score a job against the user profile.

    Returns a ScoreBreakdown with individual factor scores and total.
    """
    breakdown = ScoreBreakdown()

    # Blacklist check first
    blacklist_reason = _check_blacklist(job, profile)
    if blacklist_reason:
        breakdown.blacklist_penalty = 100.0
        breakdown.skip_reason = blacklist_reason
        breakdown.action = Action.SKIP
        breakdown.compute_total()
        return breakdown

    breakdown.title_score = _score_title(job, profile)
    breakdown.keyword_score = _score_keywords(job, profile)
    breakdown.location_score = _score_location(job, profile)
    breakdown.seniority_score = _score_seniority(job, profile)
    breakdown.employment_score = _score_employment(job, profile)

    breakdown.compute_total()

    logger.info(
        "job_scored",
        title=job.title,
        company=job.company,
        score=round(breakdown.total, 1),
    )

    return breakdown


def decide_action(
    score: float,
    auto_apply_enabled: bool = False,
    draft_only: bool = True,
    skip_reason: str | None = None,
) -> Action:
    """Decide what to do with a scored job.

    Rules:
    - If blacklisted or score < SKIP_THRESHOLD → SKIP
    - If draft_only=True (default) → always DRAFT
    - If auto_apply=True AND score >= AUTO_APPLY_THRESHOLD → AUTO_APPLY
    - Otherwise → DRAFT
    """
    if skip_reason:
        return Action.SKIP

    if score < SKIP_THRESHOLD:
        return Action.SKIP

    if draft_only:
        return Action.DRAFT

    if auto_apply_enabled and score >= AUTO_APPLY_THRESHOLD:
        return Action.AUTO_APPLY

    return Action.DRAFT
