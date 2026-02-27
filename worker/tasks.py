"""Celery task definitions — the processing pipeline.

Pipeline: process_message → process_url → score_job → generate_application → submit_application

Each task enforces proper state transitions and approval checks.
"""

from __future__ import annotations

import json
import asyncio

import structlog
from celery import shared_task

from core.config import get_settings
from core.utils import run_async
from db.models import (
    Application,
    ExtractedURL,
    Job,
    JobStatus,
    Submission,
    SubmissionStatus,
    URLStatus,
)
from db.session import get_session_factory
from ingestion.url_utils import job_signature, normalize_url, url_hash
from ingestion.whatsapp_webhook import extract_urls
from jobs.extractor import extract_jobs
from jobs.fetcher import fetch_page
from match.scoring import Action, decide_action, score_job

logger = structlog.get_logger(__name__)


def _get_db():
    """Get a DB session for use in tasks (not a FastAPI dependency)."""
    factory = get_session_factory()
    return factory()


# ── Task 1: Process a message ─────────────────────────────


@shared_task(name="worker.tasks.process_message_task", bind=True, max_retries=2)
def process_message_task(self, message_id: int):
    """Extract URLs from a stored message and enqueue URL processing."""
    db = _get_db()
    try:
        from db.models import Message
        msg = db.query(Message).filter(Message.id == message_id).first()
        if not msg:
            logger.warning("message_not_found", id=message_id)
            return

        urls = extract_urls(msg.body or "")
        enqueued = 0

        for raw_url in urls:
            normalized = normalize_url(raw_url)
            uhash = url_hash(normalized)

            # Dedup
            existing = db.query(ExtractedURL).filter(
                ExtractedURL.url_hash == uhash
            ).first()
            if existing:
                continue

            db_url = ExtractedURL(
                message_id=msg.id,
                original_url=raw_url,
                normalized_url=normalized,
                url_hash=uhash,
                status=URLStatus.PENDING,
            )
            db.add(db_url)
            db.flush()

            db.commit()

            # Chain to next task
            settings = get_settings()
            if settings.tasks_always_eager:
                process_url_task.apply(args=[db_url.id])
            else:
                process_url_task.delay(db_url.id)
            enqueued += 1

        db.commit()
        logger.info("message_processed", message_id=message_id, urls_enqueued=enqueued)

    except Exception as exc:
        db.rollback()
        logger.error("message_processing_failed", error=str(exc))
        raise self.retry(exc=exc, countdown=30)
    finally:
        db.close()


# ── Task 2: Process a URL (fetch + extract jobs) ──────────


@shared_task(name="worker.tasks.process_url_task", bind=True, max_retries=2)
def process_url_task(self, url_id: int):
    """Fetch a URL, extract job postings, and enqueue scoring."""
    db = _get_db()
    try:
        db_url = db.query(ExtractedURL).filter(ExtractedURL.id == url_id).first()
        if not db_url:
            logger.warning("url_not_found", id=url_id)
            return

        # Fetch the page
        result = fetch_page(db_url.normalized_url)

        if result.blocked:
            db_url.status = URLStatus.BLOCKED
            db_url.fetch_error = result.error
            db.commit()
            logger.warning("url_blocked", url=db_url.normalized_url, error=result.error)
            return

        if not result.success:
            db_url.status = URLStatus.FAILED
            db_url.fetch_error = result.error
            db.commit()
            logger.warning("url_fetch_failed", url=db_url.normalized_url, error=result.error)
            return

        db_url.status = URLStatus.FETCHED

        # Extract jobs
        extraction = extract_jobs(result.html, db_url.normalized_url)

        # Vision Fallback: If no jobs found, try browser-based vision extraction
        if not extraction.has_jobs:
            logger.info("try_vision_fallback", url=db_url.normalized_url)
            from jobs.extractor import extract_jobs_with_vision
            try:
                extraction = run_async(extract_jobs_with_vision(db_url.normalized_url))
            except Exception as e:
                logger.error("vision_fallback_failed", error=str(e))

        if not extraction.has_jobs:
            db.commit()
            logger.info("no_jobs_at_url", url=db_url.normalized_url)
            return

        for job_data in extraction.jobs:
            # Dedup by job signature
            sig = job_signature(
                job_data.title, job_data.company, job_data.location
            )
            existing_job = db.query(Job).filter(Job.job_signature == sig).first()
            if existing_job:
                logger.debug("duplicate_job", title=job_data.title)
                continue

            # Also dedup by apply_url
            apply_hash = url_hash(job_data.apply_url) if job_data.apply_url else None
            if apply_hash:
                existing_apply = db.query(Job).filter(
                    Job.apply_url_hash == apply_hash
                ).first()
                if existing_apply:
                    logger.debug("duplicate_apply_url", url=job_data.apply_url)
                    continue

            db_job = Job(
                extracted_url_id=db_url.id,
                title=job_data.title,
                company=job_data.company or "",
                location=job_data.location or "",
                employment_type=job_data.employment_type or "",
                seniority=job_data.seniority or "",
                description=job_data.description or "",
                requirements=job_data.requirements or "",
                apply_url=job_data.apply_url or "",
                source_url=job_data.source_url,
                date_posted=job_data.date_posted or "",
                keywords=json.dumps(job_data.keywords),
                apply_url_hash=apply_hash,
                job_signature=sig,
                status=JobStatus.EXTRACTED,
            )
            db.add(db_job)
            db.flush()

            db.commit()

            # Chain to scoring
            settings = get_settings()
            if settings.tasks_always_eager:
                score_job_task.apply(args=[db_job.id])
            else:
                score_job_task.delay(db_job.id)

        db.commit()
        logger.info(
            "url_processed",
            url=db_url.normalized_url,
            jobs_found=len(extraction.jobs),
            parser=extraction.parser_used,
        )

    except Exception as exc:
        db.rollback()
        logger.error("url_processing_failed", error=str(exc))
        raise self.retry(exc=exc, countdown=60)
    finally:
        db.close()


# ── Task 3: Score a job ───────────────────────────────────


@shared_task(name="worker.tasks.score_job_task", bind=True, max_retries=1)
def score_job_task(self, job_id: int):
    """Score a job against the user profile and decide the action."""
    from profile.loader import get_profile

    from jobs.models import JobData

    db = _get_db()
    try:
        settings = get_settings()
        db_job = db.query(Job).filter(Job.id == job_id).first()
        if not db_job:
            return

        profile = get_profile()

        # Convert DB model to JobData for scoring
        job_data = JobData(
            title=db_job.title,
            company=db_job.company,
            location=db_job.location,
            employment_type=db_job.employment_type,
            seniority=db_job.seniority,
            description=db_job.description,
            requirements=db_job.requirements,
            apply_url=db_job.apply_url,
            source_url=db_job.source_url,
            date_posted=db_job.date_posted,
            keywords=json.loads(db_job.keywords) if db_job.keywords else [],
        )

        breakdown = score_job(job_data, profile)
        action = decide_action(
            score=breakdown.total,
            auto_apply_enabled=settings.auto_apply,
            draft_only=settings.draft_only,
            skip_reason=breakdown.skip_reason,
        )

        db_job.score = breakdown.total
        db_job.status = JobStatus.SCORED

        if action == Action.SKIP:
            db_job.status = JobStatus.SKIPPED
            db.commit()
            logger.info("job_skipped", title=db_job.title, score=breakdown.total,
                        reason=breakdown.skip_reason)
            return

        # Create application draft
        db_job.status = JobStatus.DRAFT
        db.commit()

        # Chain to LLM generation
        if settings.tasks_always_eager:
            generate_application_task.apply(args=[job_id])
        else:
            generate_application_task.delay(job_id)

        logger.info("job_scored_and_queued", title=db_job.title,
                     score=breakdown.total, action=action.value)

    except Exception as exc:
        db.rollback()
        logger.error("scoring_failed", error=str(exc))
        raise self.retry(exc=exc, countdown=30)
    finally:
        db.close()


# ── Task 4: Generate application materials ────────────────


@shared_task(name="worker.tasks.generate_application_task", bind=True, max_retries=2)
def generate_application_task(self, job_id: int):
    """Generate cover letter, recruiter message, and Q&A answers via LLM."""
    import asyncio
    from profile.loader import get_profile

    from jobs.models import JobData
    from llm.generation import generate_full_application

    db = _get_db()
    try:
        db_job = db.query(Job).filter(Job.id == job_id).first()
        if not db_job:
            return

        profile = get_profile()

        job_data = JobData(
            title=db_job.title,
            company=db_job.company,
            location=db_job.location,
            employment_type=db_job.employment_type,
            seniority=db_job.seniority,
            description=db_job.description,
            requirements=db_job.requirements,
            apply_url=db_job.apply_url,
            source_url=db_job.source_url,
        )

        # Run async generation in sync context
        generated = run_async(generate_full_application(job_data, profile))

        settings = get_settings()

        # Decide whether to auto-approve immediately
        from datetime import datetime
        from match.scoring import Action, decide_action
        action = decide_action(
            score=db_job.score or 0.0,
            auto_apply_enabled=settings.auto_apply,
            draft_only=settings.draft_only,
            threshold=settings.auto_apply_threshold,
        )
        auto_approve = action == Action.AUTO_APPLY

        app = Application(
            job_id=job_id,
            cover_letter=generated.cover_letter,
            recruiter_message=generated.recruiter_message,
            qa_answers=json.dumps(generated.qa_answers),
            status=JobStatus.APPROVED if auto_approve else JobStatus.DRAFT,
            approved_at=datetime.utcnow() if auto_approve else None,
        )
        db.add(app)
        db.flush()

        if auto_approve:
            db_job.status = JobStatus.APPROVED

        db.commit()

        logger.info(
            "application_generated",
            job=db_job.title,
            score=db_job.score,
            threshold=settings.auto_apply_threshold,
            has_placeholders=generated.has_placeholders,
            auto_approved=auto_approve,
            reason="Score above threshold" if auto_approve else "Score below threshold or draft_only enabled",
        )

        # Immediately chain to submission when auto-approved
        if auto_approve:
            logger.info("auto_apply_queued", job=db_job.title, app_id=app.id)
            if settings.tasks_always_eager:
                submit_application_task.apply(args=[app.id])
            else:
                submit_application_task.delay(app.id)

    except Exception as exc:
        db.rollback()
        logger.error("generation_failed", error=str(exc))
        raise self.retry(exc=exc, countdown=60)
    finally:
        db.close()


# ── Task 5: Submit application (only if approved) ─────────


@shared_task(name="worker.tasks.submit_application_task", bind=True, max_retries=1)
def submit_application_task(self, application_id: int):
    """Submit an approved application to the job board.

    CRITICAL: Enforces that the application must be APPROVED before submission.
    Falls back to draft_only for unsupported platforms.
    """
    import asyncio
    from profile.loader import get_profile

    from jobs.models import JobData
    from submitters.ashby import AshbySubmitter
    from submitters.base import DraftOnlySubmitter, SubmitterRegistry
    from submitters.comeet import ComeetSubmitter
    from submitters.greenhouse import GreenhouseSubmitter
    from submitters.indeed import IndeedSubmitter
    from submitters.jobvite import JobviteSubmitter
    from submitters.lever import LeverSubmitter
    from submitters.linkedin import LinkedInSubmitter
    from submitters.smartrecruiters import SmartRecruitersSubmitter
    from submitters.workable import WorkableSubmitter
    from submitters.workday import WorkdaySubmitter

    db = _get_db()
    try:
        settings = get_settings()

        app = db.query(Application).filter(Application.id == application_id).first()
        if not app:
            logger.warning("application_not_found", id=application_id)
            return

        # *** APPROVAL ENFORCEMENT ***
        if app.status != JobStatus.APPROVED:
            logger.warning(
                "submission_blocked_not_approved",
                application_id=application_id,
                status=app.status.value if app.status else "unknown",
            )
            return

        db_job = app.job
        if not db_job:
            return

        profile = get_profile()

        from submitters.icims import IcimsSubmitter
        from llm.generation import GeneratedApplication

        # Build ordered submitter list — Tier 1 (API), Tier 2 (browser), Tier 3 (draft)
        all_submitters = [
            # Tier 1: Official public APIs (most reliable, no credentials needed for many)
            GreenhouseSubmitter(api_key=settings.greenhouse_api_key),
            LeverSubmitter(api_key=settings.lever_api_key),
            AshbySubmitter(),
            WorkableSubmitter(),
            SmartRecruitersSubmitter(api_key=settings.smartrecruiters_api_key),
            JobviteSubmitter(),
            # Tier 2: Browser automation (Playwright)
            LinkedInSubmitter(
                cookies_file=settings.linkedin_cookies_file,
                email=settings.linkedin_email,
                password=settings.linkedin_password,
            ),
            IndeedSubmitter(
                cookies_file=settings.indeed_cookies_file,
                email=settings.indeed_email,
                password=settings.indeed_password,
            ),
            IcimsSubmitter(),
            ComeetSubmitter(),
            # Tier 3: Draft-only — Workday SSO wall, never auto-submit
            WorkdaySubmitter(),
        ]

        job_ref = JobData(
            title=db_job.title, company=db_job.company,
            location=db_job.location, apply_url=db_job.apply_url,
            source_url=db_job.source_url,
        )

        generated = GeneratedApplication(
            cover_letter=app.cover_letter or "",
            recruiter_message=app.recruiter_message or "",
            qa_answers=json.loads(app.qa_answers) if app.qa_answers else {},
        )

        profile_dict = profile.model_dump()
        resume_path  = profile.resume.pdf_path or None

        # Cascade: try each matching submitter, stop on first success
        result = None
        if settings.draft_only:
            result = run_async(DraftOnlySubmitter().submit(job_ref, generated, profile_dict, resume_path))
        else:
            for sub in all_submitters:
                if not sub.can_submit(job_ref):
                    continue
                attempt = run_async(sub.submit(job_ref, generated, profile_dict, resume_path))
                logger.info(
                    "submitter_attempt",
                    platform=sub.platform_name,
                    status=attempt.status,
                    success=attempt.success,
                )
                if attempt.success and attempt.status == "submitted":
                    result = attempt
                    break
                if result is None or attempt.status != "failed":
                    # Keep best result seen (prefer draft_only over failed)
                    result = attempt

        # Always fall back to draft_only if no real submission succeeded
        if result is None or result.status == "failed":
            result = run_async(DraftOnlySubmitter().submit(job_ref, generated, profile_dict, resume_path))

        # Record submission
        from datetime import datetime as _dt
        if result.status == "submitted" and result.success:
            sub_status = SubmissionStatus.SUCCESS
        elif result.status == "draft_only":
            sub_status = SubmissionStatus.DRAFT_ONLY
        else:
            sub_status = SubmissionStatus.FAILED

        submission = Submission(
            application_id=application_id,
            submitter_name=result.platform,
            status=sub_status,
            confirmation_url=result.confirmation_url,
            confirmation_id=result.confirmation_id,
            error_message=result.error,
            submitted_at=_dt.utcnow() if result.success and result.status == "submitted" else None,
        )
        db.add(submission)

        # Job status mirrors submission outcome
        if result.status == "submitted" and result.success:
            db_job.status = JobStatus.SUBMITTED
        elif result.status in ("draft_only", "captcha_blocked"):
            db_job.status = JobStatus.DRAFT
        else:
            db_job.status = JobStatus.FAILED
        db.commit()

        logger.info(
            "submission_completed",
            job=db_job.title,
            platform=result.platform,
            status=result.status,
            success=result.success,
        )

    except Exception as exc:
        db.rollback()
        logger.error("submission_failed", error=str(exc))
        raise self.retry(exc=exc, countdown=60)
    finally:
        db.close()
