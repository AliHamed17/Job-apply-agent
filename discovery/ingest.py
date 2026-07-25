"""Shared ingestion path for jobs returned by discovery providers."""

from __future__ import annotations

import json

import structlog

from db.models import Job, JobStatus
from ingestion.url_utils import job_signature, url_hash
from jobs.models import JobData

logger = structlog.get_logger(__name__)


def ingest_discovered_jobs(
    db,
    jobs: list[JobData],
    *,
    source: str,
    easy_apply: bool,
    tasks_always_eager: bool,
) -> int:
    """Insert deduplicated jobs and enqueue the existing scoring pipeline."""
    from worker.tasks import score_job_task

    inserted = 0
    for job_data in jobs:
        signature = job_signature(job_data.title, job_data.company, job_data.location)
        if db.query(Job).filter(Job.job_signature == signature).first():
            logger.debug("discovery_duplicate_job", source=source, title=job_data.title)
            continue

        apply_hash = url_hash(job_data.apply_url) if job_data.apply_url else None
        if apply_hash and db.query(Job).filter(Job.apply_url_hash == apply_hash).first():
            logger.debug("discovery_duplicate_apply_url", source=source)
            continue

        try:
            db_job = Job(
                extracted_url_id=None,
                title=job_data.title,
                company=job_data.company or "",
                location=job_data.location or "",
                employment_type=job_data.employment_type or "",
                seniority=job_data.seniority or "",
                description=job_data.description or "",
                requirements=job_data.requirements or "",
                apply_url=job_data.apply_url or "",
                source_url=job_data.source_url or job_data.apply_url or "",
                date_posted=job_data.date_posted or "",
                keywords=json.dumps(job_data.keywords),
                apply_url_hash=apply_hash,
                job_signature=signature,
                status=JobStatus.EXTRACTED,
                discovery_source=source,
                easy_apply=easy_apply,
            )
            db.add(db_job)
            db.flush()
            db.commit()

            if tasks_always_eager:
                score_job_task.apply(args=[db_job.id])
            else:
                score_job_task.delay(db_job.id)
            inserted += 1
        except Exception:
            db.rollback()
            logger.exception("discovery_job_insert_failed", source=source)

    return inserted
