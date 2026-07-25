"""Celery task definitions — the processing pipeline.

Pipeline: process_message → process_url → score_job → generate_application → submit_application

Each task enforces proper state transitions and approval checks.
"""

from __future__ import annotations

import json

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
            min_apply_score=settings.min_apply_score,
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

        settings = get_settings()

        from pathlib import Path
        from profile.cv_routing import (
            RoutingDecision,
            RoutingJob,
            load_routing_config,
            parse_required_skills,
            route_cv,
        )

        routing_job = RoutingJob(
            title=db_job.title or "",
            description=" ".join(
                filter(None, [db_job.description, db_job.requirements])
            ),
            seniority=db_job.seniority or "",
            required_skills=parse_required_skills(db_job.keywords),
        )

        routing_path = Path(settings.cv_routing_path)
        if routing_path.exists():
            routing_config = load_routing_config(routing_path)
            routing = route_cv(routing_job, routing_config)
            if (
                settings.llm_cv_routing
                and not routing.overridden
                and routing.fallback_reason in {
                    "confidence_below_threshold",
                    "abstained_low_confidence",
                }
            ):
                from profile.cv_routing_llm import load_cv_excerpts, select_cv_via_llm

                try:
                    excerpts = load_cv_excerpts(
                        routing_config, settings.cv_directory, settings.cv_routing_path
                    )
                    llm_routing = run_async(
                        select_cv_via_llm(routing_job, routing_config, excerpts)
                    )
                    if llm_routing.selected_cv_id is not None:
                        llm_routing.matched_evidence = [
                            *routing.matched_evidence,
                            *llm_routing.matched_evidence,
                        ]
                        routing = llm_routing
                except Exception as exc:
                    logger.warning("llm_cv_routing_unavailable", error=str(exc))
        else:
            routing = RoutingDecision(
                selected_cv_id=None,
                selected_file=None,
                confidence=0,
                matched_evidence=[],
                fallback_reason="routing_not_configured",
            )

        # Get selected CV text for application generation
        cv_text = None
        if routing.selected_cv_id:
            from profile.cv_content_cache import get_cv_text_by_id
            cv_text = get_cv_text_by_id(
                routing.selected_cv_id,
                cv_routing_path=settings.cv_routing_path,
                cv_directory=settings.cv_directory,
            )

        # Run async generation in sync context using the selected CV text
        generated = run_async(generate_full_application(job_data, profile, cv_text=cv_text))

        # Decide whether to auto-approve immediately
        from datetime import datetime

        from match.scoring import Action, decide_action
        action = decide_action(
            score=db_job.score or 0.0,
            auto_apply_enabled=settings.auto_apply,
            draft_only=settings.draft_only,
            threshold=settings.auto_apply_threshold,
            min_apply_score=settings.min_apply_score,
        )
        # Unfilled [PLACEHOLDER: ...] markers and uncertain CV routes must
        # never reach an employer without review.
        auto_approve = (
            action == Action.AUTO_APPLY
            and routing.selected_cv_id is not None
            and routing.fallback_reason is None
            and not generated.has_placeholders
        )

        from db.models import UserProfileVersion

        latest_profile = (
            db.query(UserProfileVersion)
            .order_by(UserProfileVersion.version.desc())
            .first()
        )
        profile_version = latest_profile.version if latest_profile else None

        # Application.job_id is UNIQUE — this task can run more than once for
        # the same job (a regenerate action, or Celery's own retry landing
        # after a transient error on a later line). Update the existing row
        # in place instead of blindly inserting, which used to raise
        # IntegrityError and burn a full (real, non-mock) LLM generation on
        # every retry without ever persisting the result.
        app = db.query(Application).filter(Application.job_id == job_id).first()
        if app is None:
            app = Application(job_id=job_id)
            db.add(app)

        app.cover_letter = generated.cover_letter
        app.recruiter_message = generated.recruiter_message
        app.qa_answers = json.dumps(generated.qa_answers)
        app.status = JobStatus.APPROVED if auto_approve else JobStatus.DRAFT
        app.approved_at = datetime.utcnow() if auto_approve else None
        app.selected_cv_id = routing.selected_cv_id
        app.cv_routing_confidence = routing.confidence
        app.cv_routing_evidence = json.dumps(routing.matched_evidence)
        app.cv_routing_fallback_reason = routing.fallback_reason
        if routing.selected_cv_id is None or routing.fallback_reason:
            app.needs_review_reason = "CV_ROUTING_REVIEW_REQUIRED"
        elif generated.has_placeholders:
            fields = ", ".join(generated.placeholder_fields[:3]) or "unspecified"
            app.needs_review_reason = f"UNFILLED_PLACEHOLDERS:{fields}"
        else:
            app.needs_review_reason = None
        app.profile_version = profile_version
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
            reason=(
                "Score above threshold"
                if auto_approve
                else "Score below threshold, draft-only, or CV routing review required"
            ),
        )

        # ── Notify originating WhatsApp sender (Cloud API) ────────────────
        # Only when draft (not auto-approved) and Cloud API is configured
        if not auto_approve and settings.whatsapp_api_token and settings.whatsapp_phone_number_id:
            try:
                # Walk: Job → ExtractedURL → Message to get sender
                url_record = (
                    db.query(ExtractedURL)
                    .filter(ExtractedURL.id == db_job.extracted_url_id)
                    .first()
                )
                if url_record and url_record.message:
                    sender = url_record.message.sender_phone
                    if sender and sender not in ("manual", "whatsapp-bridge", "dashboard"):
                        from api.routes.webhook import _send_approval_buttons
                        run_async(_send_approval_buttons(
                            sender,
                            job_id,
                            db_job.title,
                            db_job.company,
                            db_job.score or 0.0,
                            settings,
                        ))
                        logger.info(
                            "whatsapp_approval_sent",
                            job=db_job.title,
                            sender=sender,
                        )
            except Exception as notify_err:
                logger.warning("whatsapp_notify_failed", error=str(notify_err))

        # Immediately chain to submission only in local/eager dev mode.
        # With a real broker, the application stays APPROVED and the
        # priority drainer (Task 3.6, worker.drainer.drain_apply_queue_task)
        # submits it highest-score-first under governor pacing.
        if auto_approve:
            if settings.tasks_always_eager:
                logger.info("auto_apply_queued", job=db_job.title, app_id=app.id)
                submit_application_task.apply(args=[app.id])
            else:
                logger.info("auto_apply_queued_for_drainer", job=db_job.title, app_id=app.id)

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
    from profile.loader import get_profile

    from jobs.models import JobData
    from submitters.ashby import AshbySubmitter
    from submitters.base import DraftOnlySubmitter
    from submitters.comeet import ComeetSubmitter
    from submitters.greenhouse import GreenhouseSubmitter
    from submitters.indeed import IndeedSubmitter
    from submitters.jobvite import JobviteSubmitter
    from submitters.lever import LeverSubmitter
    from submitters.linkedin_v2 import LinkedInV2Submitter
    from submitters.smartrecruiters import SmartRecruitersSubmitter
    from submitters.workable import WorkableSubmitter
    from submitters.workday import WorkdaySubmitter

    db = _get_db()
    attempt = None
    try:
        settings = get_settings()

        from worker.submission_attempts import claim_attempt

        attempt = claim_attempt(db, application_id)
        if attempt is None:
            logger.info("submission_claim_skipped", application_id=application_id)
            return
        app = attempt.application

        db_job = app.job
        if not db_job:
            from worker.submission_attempts import mark_attempt_unknown

            mark_attempt_unknown(db, attempt, "JOB_MISSING")
            return

        profile = get_profile()

        from core.governor import get_governor
        from llm.generation import GeneratedApplication
        from submitters.icims import IcimsSubmitter

        governor = get_governor()

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
            LinkedInV2Submitter(db=db),
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
        resume_path = profile.resume.pdf_path or None
        from pathlib import Path

        from db.models import UserProfileVersion

        latest_profile = (
            db.query(UserProfileVersion)
            .order_by(UserProfileVersion.version.desc())
            .first()
        )
        if app.selected_cv_id:
            from profile.cv_routing import load_routing_config

            try:
                routing_config = load_routing_config(settings.cv_routing_path)
                cv = next(
                    (
                        item
                        for item in routing_config.cvs
                        if item.id == app.selected_cv_id
                    ),
                    None,
                )
                root = Path(settings.cv_directory).resolve()
                candidate = (root / cv.file).resolve() if cv else None
            except (FileNotFoundError, ValueError):
                candidate = None
                root = Path(settings.cv_directory).resolve()
            if candidate and candidate.parent == root and candidate.is_file():
                resume_path = str(candidate)
            else:
                from datetime import UTC as _UTC
                from datetime import datetime as _dt

                attempt.status = SubmissionStatus.FAILED
                attempt.reason_code = "SELECTED_CV_UNAVAILABLE"
                attempt.finished_at = _dt.now(_UTC).replace(tzinfo=None)
                app.status = JobStatus.NEEDS_REVIEW
                app.needs_review_reason = "SELECTED_CV_UNAVAILABLE"
                if app.job:
                    app.job.status = JobStatus.NEEDS_REVIEW
                db.commit()
                return
        attempt.selected_cv_id = app.selected_cv_id or (
            Path(resume_path).name if resume_path else None
        )
        attempt.profile_version = app.profile_version or (
            latest_profile.version if latest_profile else None
        )
        db.commit()

        # Cascade: try each matching submitter, stop on first success
        result = None
        if settings.draft_only:
            result = run_async(
                DraftOnlySubmitter().submit(
                    job_ref, generated, profile_dict, resume_path
                )
            )
        else:
            for sub in all_submitters:
                if not sub.can_submit(job_ref):
                    continue
                if isinstance(sub, LinkedInV2Submitter):
                    # can_apply_linkedin() adds the inter-action gap on top of
                    # can_act(), so a submission enqueued directly (e.g. the
                    # approve endpoint) can't run back-to-back inside the gap.
                    ok, reason = governor.can_apply_linkedin()
                    if not ok:
                        from datetime import UTC as _UTC
                        from datetime import datetime as _dt

                        attempt.status = SubmissionStatus.FAILED
                        attempt.reason_code = "GOVERNOR_DEFERRED"
                        attempt.finished_at = _dt.now(_UTC).replace(tzinfo=None)
                        db.commit()
                        logger.info(
                            "linkedin_submit_deferred",
                            application_id=application_id,
                            job=db_job.title,
                            reason=reason,
                        )
                        # Leave the application APPROVED — the drainer
                        # (Task 3.6) will retry once the governor allows it.
                        return
                # NOT `attempt` — that name holds the claimed Submission ORM
                # row, and rebinding it here would leave the row unfinalized
                # below (and break the except-handler's db.get on .id).
                sub_result = run_async(
                    sub.submit(job_ref, generated, profile_dict, resume_path)
                )
                logger.info(
                    "submitter_attempt",
                    platform=sub.platform_name,
                    status=sub_result.status,
                    success=sub_result.success,
                )
                if sub_result.success and sub_result.status == "submitted":
                    result = sub_result
                    break
                if result is None or sub_result.status != "failed":
                    # Keep best result seen (prefer draft_only over failed)
                    result = sub_result

        # abort-don't-lie: a blocked required field is surfaced as
        # NEEDS_REVIEW rather than silently drafted or failed.
        #
        # Read this BEFORE the draft fallback below. The fallback replaces
        # `result` wholesale with DraftOnlySubmitter's (error=None), so
        # extracting afterwards silently dropped the reason for any submitter
        # that reported the block as status="failed" — the application landed
        # in DRAFT with no record of which question stopped it.
        needs_review_reason = None
        if result is not None and result.error and result.error.startswith("NEEDS_REVIEW:"):
            needs_review_reason = result.error.split("NEEDS_REVIEW:", 1)[1]

        # Always fall back to draft_only if no real submission succeeded
        if result is None or result.status == "failed":
            result = run_async(
                DraftOnlySubmitter().submit(
                    job_ref, generated, profile_dict, resume_path
                )
            )

        # Finalize the pre-committed attempt. No external action can run before
        # this attempt exists, so task redelivery cannot duplicate the action.
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from worker.submission_attempts import (
            classify_reason,
            redacted_diagnostics,
        )

        if result.status == "submitted" and result.success:
            sub_status = SubmissionStatus.SUCCESS
        elif result.status == "draft_only":
            sub_status = SubmissionStatus.DRAFT_ONLY
        else:
            sub_status = SubmissionStatus.FAILED

        now = _dt.now(_UTC).replace(tzinfo=None)
        attempt.submitter_name = result.platform
        attempt.status = sub_status
        attempt.confirmation_url = result.confirmation_url
        attempt.confirmation_id = result.confirmation_id
        attempt.error_message = None
        attempt.reason_code = classify_reason(result.error, result.status)
        attempt.diagnostic_details = redacted_diagnostics(result.error)
        attempt.finished_at = now
        attempt.submitted_at = (
            now if result.success and result.status == "submitted" else None
        )

        # Job/application status mirrors submission outcome.
        #
        # CRITICAL: app.status must leave APPROVED on every one of these
        # branches. The drainer (worker.drainer.select_next_application)
        # re-selects any Application still APPROVED, so if a completed
        # attempt left it APPROVED the same app would be re-submitted
        # every drain tick — re-driving a live LinkedIn apply and hitting
        # the Submission.application_id UNIQUE constraint (IntegrityError
        # retry loop), starving every other approved job behind it.
        if result.status == "submitted" and result.success:
            db_job.status = JobStatus.SUBMITTED
            app.status = JobStatus.SUBMITTED
            if result.platform == "linkedin":
                governor.record_application()
                app.submission_channel = "linkedin_easy"
        elif needs_review_reason is not None:
            app.needs_review_reason = needs_review_reason
            app.status = JobStatus.NEEDS_REVIEW
            db_job.status = JobStatus.NEEDS_REVIEW
        elif result.status in ("draft_only", "captcha_blocked"):
            db_job.status = JobStatus.DRAFT
            app.status = JobStatus.DRAFT
        else:
            db_job.status = JobStatus.FAILED
            app.status = JobStatus.FAILED
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
        logger.error("submission_failed", error=type(exc).__name__)
        if attempt is not None:
            from worker.submission_attempts import mark_attempt_unknown

            refreshed = db.get(Submission, attempt.id)
            if refreshed is not None:
                mark_attempt_unknown(db, refreshed, "WORKER_EXCEPTION_INDETERMINATE")
        return
    finally:
        db.close()
