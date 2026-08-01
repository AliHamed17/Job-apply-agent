"""Immutable persistence for privacy-bounded job-fit decisions."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from db.models import JobFitDecisionRecord
from match.job_fit import JobFitDecisionV1


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def persist_job_fit_decision(
    db: Session,
    *,
    job_id: int,
    decision: JobFitDecisionV1,
) -> JobFitDecisionRecord:
    """Insert once for the exact decision payload; never update historical rows."""

    digest = decision.decision_digest
    existing = (
        db.query(JobFitDecisionRecord)
        .filter(
            JobFitDecisionRecord.job_id == job_id,
            JobFitDecisionRecord.decision_digest == digest,
        )
        .one_or_none()
    )
    if existing is not None:
        # Validate the full immutable payload before reusing a digest. This
        # also protects against accidental persistence-contract drift.
        if decision_from_record(existing) != decision:
            raise ValueError("JOB_FIT_DECISION_DIGEST_COLLISION")
        return existing

    record = JobFitDecisionRecord(
        job_id=job_id,
        decision_digest=digest,
        job_digest=decision.job_digest,
        profile_version=decision.profile_version,
        routing_config_digest=decision.routing_config_digest,
        cv_manifest_digest=decision.cv_manifest_digest,
        selected_cv_id=decision.selected_cv_id,
        selected_cv_hash=decision.selected_cv_hash,
        routing_confidence=decision.routing_confidence,
        routing_margin=decision.routing_margin,
        routing_fallback_reason=decision.routing_fallback_reason,
        fit_score=decision.fit_score,
        disposition=decision.disposition.value,
        quality_eligible=decision.quality_eligible,
        hard_exclusions_json=_compact(decision.hard_exclusions),
        uncertainty_json=_compact(decision.uncertainty),
        unsupported_skills_json=_compact(decision.unsupported_required_skills),
        evidence_json=_compact([item.model_dump(mode="json") for item in decision.evidence]),
        thresholds_json=_compact(decision.thresholds.model_dump(mode="json")),
        policy_version=decision.policy_version,
        model_identity=decision.model_identity,
        qualification_digest=decision.qualification_digest,
    )
    db.add(record)
    db.flush()
    return record


def decision_from_record(record: JobFitDecisionRecord) -> JobFitDecisionV1:
    """Reconstruct and schema-validate one persisted immutable decision."""

    return JobFitDecisionV1.model_validate(
        {
            "job_digest": record.job_digest,
            "profile_version": record.profile_version,
            "routing_config_digest": record.routing_config_digest,
            "cv_manifest_digest": record.cv_manifest_digest,
            "selected_cv_id": record.selected_cv_id,
            "selected_cv_hash": record.selected_cv_hash,
            "routing_confidence": record.routing_confidence,
            "routing_margin": record.routing_margin,
            "routing_fallback_reason": record.routing_fallback_reason,
            "fit_score": record.fit_score,
            "disposition": record.disposition,
            "quality_eligible": record.quality_eligible,
            "hard_exclusions": json.loads(record.hard_exclusions_json),
            "uncertainty": json.loads(record.uncertainty_json),
            "unsupported_required_skills": json.loads(record.unsupported_skills_json),
            "evidence": json.loads(record.evidence_json),
            "thresholds": json.loads(record.thresholds_json),
            "policy_version": record.policy_version,
            "model_identity": record.model_identity,
            "qualification_digest": record.qualification_digest,
        }
    )


def latest_job_fit_decision(
    db: Session,
    *,
    job_id: int,
) -> tuple[JobFitDecisionRecord, JobFitDecisionV1] | None:
    record = (
        db.query(JobFitDecisionRecord)
        .filter(JobFitDecisionRecord.job_id == job_id)
        .order_by(JobFitDecisionRecord.created_at.desc(), JobFitDecisionRecord.id.desc())
        .first()
    )
    return (record, decision_from_record(record)) if record is not None else None
