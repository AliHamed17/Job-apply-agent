"""Job posting authenticity & ghost listing detector."""

from __future__ import annotations

from dataclasses import dataclass, field
import structlog

from jobs.models import JobData

logger = structlog.get_logger(__name__)


@dataclass
class GhostDetectionResult:
    is_ghost_suspect: bool = False
    risk_score: float = 0.0
    reasons: list[str] = field(default_factory=list)


def detect_ghost_posting(job: JobData) -> GhostDetectionResult:
    """Analyze job posting signals for ghost listing indicators."""
    reasons: list[str] = []
    risk_score = 0.0

    desc = (job.description or "").lower()
    title = (job.title or "").lower()

    if len(desc) < 200:
        risk_score += 0.3
        reasons.append("Posting description is unusually short (< 200 characters)")

    if any(phrase in desc for phrase in ["confidential client", "top tier client", "evergreen requisition"]):
        risk_score += 0.4
        reasons.append("Contains generic agency template indicator (e.g. 'confidential client')")

    if "reposted" in desc or "reposted" in title:
        risk_score += 0.2
        reasons.append("Posting explicitly marked as reposted")

    is_suspect = risk_score >= 0.4

    return GhostDetectionResult(
        is_ghost_suspect=is_suspect,
        risk_score=min(round(risk_score, 2), 1.0),
        reasons=reasons,
    )
