"""Application outcome tracking and scoring self-tuning module."""

from __future__ import annotations

import structlog
from db.models import Application

logger = structlog.get_logger(__name__)

OUTCOME_SUBMITTED = "submitted"
OUTCOME_INTERVIEW = "interview_invited"
OUTCOME_REJECTED = "rejected"
OUTCOME_OFFER = "offer"


def compute_outcome_boosts(db) -> dict[str, float]:
    """Calculate CV ID score multipliers based on historical interview outcomes.

    Returns:
        dict mapping cv_id -> multiplier boost float.
    """
    try:
        apps = db.query(Application).filter(Application.outcome.isnot(None)).all()
        boosts: dict[str, float] = {}
        for app in apps:
            if app.outcome in (OUTCOME_INTERVIEW, OUTCOME_OFFER):
                if app.selected_cv_id:
                    boosts[app.selected_cv_id] = boosts.get(app.selected_cv_id, 1.0) + 0.15
        return boosts
    except Exception as exc:
        logger.warning("compute_outcome_boosts_failed", error=str(exc))
        return {}
