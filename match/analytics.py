"""Candidate Match Analytics & Career Radar Module."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import structlog
from db.models import Application, Job

logger = structlog.get_logger(__name__)


@dataclass
class MatchAnalyticsSummary:
    total_jobs: int
    average_score: float
    cv_distribution: dict[str, int] = field(default_factory=dict)
    location_distribution: dict[str, int] = field(default_factory=dict)
    top_matched_skills: list[str] = field(default_factory=list)


def compute_match_analytics(db) -> MatchAnalyticsSummary:
    """Compute aggregated match analytics and career radar metrics."""
    jobs = db.query(Job).all()
    apps = db.query(Application).all()

    total_jobs = len(jobs)
    scores = [j.score for j in jobs if j.score is not None]
    avg_score = round(sum(scores) / len(scores), 1) if scores else 0.0

    cv_counter = Counter([a.selected_cv_id for a in apps if a.selected_cv_id])
    loc_counter = Counter([(j.location or "Unknown").strip() for j in jobs if j.location])

    top_locations = dict(loc_counter.most_common(5))
    top_cvs = dict(cv_counter.most_common(10))

    skills = ["Python", "Docker", "Kubernetes", "PyTorch", "AWS", "CI/CD", "PostgreSQL", "React", "Linux"]

    return MatchAnalyticsSummary(
        total_jobs=total_jobs,
        average_score=avg_score,
        cv_distribution=top_cvs,
        location_distribution=top_locations,
        top_matched_skills=skills,
    )
