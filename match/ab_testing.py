"""Autonomous Application A/B Testing Engine."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import structlog
from db.models import Application

logger = structlog.get_logger(__name__)


@dataclass
class ABTestVariantStats:
    cv_id: str
    total_applications: int
    interviews_count: int
    conversion_rate_pct: float


@dataclass
class ABTestingReport:
    total_analyzed: int
    winning_cv_id: str | None
    variants: list[ABTestVariantStats] = field(default_factory=list)


def compute_ab_test_analytics(db) -> ABTestingReport:
    """Compute callback rates and winning CV variants across all submitted applications."""
    apps = db.query(Application).all()

    cv_totals = defaultdict(int)
    cv_interviews = defaultdict(int)

    for app in apps:
        if not app.selected_cv_id:
            continue
        cv_id = app.selected_cv_id
        cv_totals[cv_id] += 1
        if getattr(app, "outcome", None) == "interview_invited":
            cv_interviews[cv_id] += 1

    variants = []
    winning_cv = None
    best_rate = -1.0

    for cv_id, total in cv_totals.items():
        interviews = cv_interviews[cv_id]
        rate = round((interviews / total * 100), 1) if total > 0 else 0.0
        variants.append(
            ABTestVariantStats(
                cv_id=cv_id,
                total_applications=total,
                interviews_count=interviews,
                conversion_rate_pct=rate,
            )
        )
        if rate > best_rate:
            best_rate = rate
            winning_cv = cv_id

    variants.sort(key=lambda v: v.conversion_rate_pct, reverse=True)

    return ABTestingReport(
        total_analyzed=len(apps),
        winning_cv_id=winning_cv,
        variants=variants,
    )
