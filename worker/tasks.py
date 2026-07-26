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


def _validated_submit_command_available(_db, _application_id: int) -> bool:
    """Fail closed until PR2 adds a durable, permit-backed command lookup."""
    return False


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
            existing = db.query(ExtractedURL).filter(ExtractedURL.url_hash == uhash).first()
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
            sig = job_signature(job_data.title, job_data.company, job_data.location)
            existing_job = db.query(Job).filter(Job.job_signature == sig).first()
            if existing_job:
                logger.debug("duplicate_job", title=job_data.title)
                continue

            # Also dedup by apply_url
            apply_hash = url_hash(job_data.apply_url) if job_data.apply_url else None
            if apply_hash:
                existing_apply = db.query(Job).filter(Job.apply_url_hash == apply_hash).first()
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
        app = db.query(Application).filter(Application.job_id == job_id).first()
        if app is not None and app.status in {
            JobStatus.SUBMITTED,
            JobStatus.SKIPPED,
        }:
            logger.warning(
                "terminal_application_regeneration_blocked",
                application_id=app.id,
                status=app.status.value,
            )
            return
        expected_application_id = app.id if app is not None else None
        expected_application_revision = int(app.revision or 1) if app is not None else None
        expected_job_status = db_job.status
        job_title = db_job.title

        profile = get_profile()

        # Convert DB model to JobData for scoring
        job_data = JobData(
            title=db_job.title or "",
            company=db_job.company or "",
            location=db_job.location or "",
            employment_type=db_job.employment_type or "",
            seniority=db_job.seniority or "",
            description=db_job.description or "",
            requirements=db_job.requirements or "",
            apply_url=db_job.apply_url or "",
            source_url=db_job.source_url,
            date_posted=db_job.date_posted or "",
            keywords=json.loads(db_job.keywords) if db_job.keywords else [],
        )

        # Scoring can be CPU-heavy and can invoke profile loaders.  Keep no
        # database transaction open while it runs, then re-lock the current
        # Application -> Job state immediately before any persistence.
        db.rollback()

        breakdown = score_job(job_data, profile)
        action = decide_action(
            score=breakdown.total,
            auto_apply_enabled=settings.auto_apply,
            draft_only=settings.draft_only,
            skip_reason=breakdown.skip_reason,
            min_apply_score=settings.min_apply_score,
        )

        from core.application_mutations import (
            ApplicationMutationBlockedError,
            ApplicationMutationIntent,
            lock_application_for_mutation,
            lock_job_without_application_for_mutation,
        )

        if expected_application_id is not None:
            try:
                locked = lock_application_for_mutation(
                    db,
                    application_id=expected_application_id,
                    intent=ApplicationMutationIntent.CONTENT,
                    expected_revision=expected_application_revision,
                )
            except ApplicationMutationBlockedError as exc:
                db.rollback()
                logger.warning(
                    "job_scoring_write_blocked",
                    job_id=job_id,
                    application_id=expected_application_id,
                    reason_code=exc.reason_code,
                )
                return
            assert locked is not None and locked.job is not None
            # Once an application exists its lifecycle is authoritative.  A
            # rescore may refresh the numeric score under the app-first lock,
            # but it must not rewrite status or enqueue regeneration.
            locked.job.score = breakdown.total
            db.commit()
            logger.info(
                "existing_application_rescored",
                job_id=job_id,
                application_id=expected_application_id,
                score=breakdown.total,
            )
            return

        try:
            db_job = lock_job_without_application_for_mutation(db, job_id=job_id)
        except ApplicationMutationBlockedError as exc:
            db.rollback()
            logger.warning(
                "job_scoring_write_blocked",
                job_id=job_id,
                reason_code=exc.reason_code,
            )
            return
        if db_job.status != expected_job_status:
            db.rollback()
            logger.warning(
                "job_scoring_write_blocked",
                job_id=job_id,
                reason_code="JOB_STATUS_CHANGED",
            )
            return

        db_job.score = breakdown.total

        if action == Action.SKIP:
            db_job.status = JobStatus.SKIPPED
            db.commit()
            logger.info(
                "job_skipped",
                title=job_title,
                score=breakdown.total,
                reason=breakdown.skip_reason,
            )
            return

        # Create application draft
        db_job.status = JobStatus.DRAFT
        db.commit()

        # Chain to LLM generation
        if settings.tasks_always_eager:
            generate_application_task.apply(args=[job_id])
        else:
            generate_application_task.delay(job_id)

        logger.info(
            "job_scored_and_queued",
            title=job_title,
            score=breakdown.total,
            action=action.value,
        )

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
    from profile.versioned_snapshot import load_versioned_profile_snapshot

    from jobs.models import JobData
    from llm.generation import generate_full_application

    db = _get_db()
    try:
        db_job = db.query(Job).filter(Job.id == job_id).first()
        if not db_job:
            return
        app = db.query(Application).filter(Application.job_id == job_id).first()
        if app is not None and app.status in {
            JobStatus.SUBMITTED,
            JobStatus.SKIPPED,
        }:
            logger.warning(
                "terminal_application_regeneration_blocked",
                application_id=app.id,
                status=app.status.value,
            )
            return
        expected_application_id = app.id if app is not None else None
        expected_revision = int(app.revision or 1) if app is not None else None

        from db.models import UserProfileVersion

        profile_snapshot = load_versioned_profile_snapshot(db)
        expected_profile_version = profile_snapshot.version
        profile = profile_snapshot.profile

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
            description=" ".join(filter(None, [db_job.description, db_job.requirements])),
            seniority=db_job.seniority or "",
            required_skills=parse_required_skills(db_job.keywords),
        )
        job_score = db_job.score or 0.0

        # Do not keep a transaction (and especially not a row lock) open while
        # local routing and LLM generation run.  The application revision and
        # submission lifecycle are locked and rechecked at the final write.
        db.rollback()

        routing_path = Path(settings.cv_routing_path)
        if routing_path.exists():
            routing_config = load_routing_config(routing_path)
            routing = route_cv(routing_job, routing_config)
            if (
                settings.llm_cv_routing
                and not routing.overridden
                and routing.fallback_reason
                in {
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

        from match.scoring import Action, decide_action

        action = decide_action(
            score=job_score,
            auto_apply_enabled=settings.auto_apply,
            draft_only=settings.draft_only,
            threshold=settings.auto_apply_threshold,
            min_apply_score=settings.min_apply_score,
        )
        # Unfilled [PLACEHOLDER: ...] markers and uncertain CV routes must
        # never reach an employer without review.
        auto_eligible = (
            action == Action.AUTO_APPLY
            and routing.selected_cv_id is not None
            and routing.fallback_reason is None
            and not generated.has_placeholders
        )

        # Application.job_id is UNIQUE — this task can run more than once for
        # the same job (a regenerate action, or Celery's own retry landing
        # after a transient error on a later line). Update the existing row
        # in place instead of blindly inserting, which used to raise
        # IntegrityError and burn a full (real, non-mock) LLM generation on
        # every retry without ever persisting the result.
        from core.application_mutations import (
            ApplicationMutationBlockedError,
            ApplicationMutationIntent,
            lock_application_for_mutation,
            lock_job_without_application_for_mutation,
        )

        if expected_application_id is None:
            try:
                db_job = lock_job_without_application_for_mutation(db, job_id=job_id)
            except ApplicationMutationBlockedError as exc:
                db.rollback()
                logger.warning(
                    "application_generation_write_blocked",
                    job_id=job_id,
                    reason_code=exc.reason_code,
                )
                return
            app = Application(job_id=job_id)
            db.add(app)
        else:
            try:
                locked = lock_application_for_mutation(
                    db,
                    application_id=expected_application_id,
                    intent=ApplicationMutationIntent.CONTENT,
                    expected_revision=expected_revision,
                )
            except ApplicationMutationBlockedError as exc:
                db.rollback()
                logger.warning(
                    "application_generation_write_blocked",
                    application_id=expected_application_id,
                    reason_code=exc.reason_code,
                )
                return
            assert locked is not None and locked.job is not None
            app = locked.application
            db_job = locked.job
            from core.application_revision import bump_application_revision

            bump_application_revision(
                db,
                app,
                reason_code="APPLICATION_REGENERATED",
            )

        latest_profile = (
            db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
        )
        current_profile_version = latest_profile.version if latest_profile else None
        if current_profile_version != expected_profile_version:
            db.rollback()
            logger.warning(
                "application_generation_write_blocked",
                job_id=job_id,
                reason_code="PROFILE_VERSION_CHANGED",
            )
            return

        app.cover_letter = generated.cover_letter
        app.recruiter_message = generated.recruiter_message
        app.qa_answers = json.dumps(generated.qa_answers)
        # A score can make an application eligible for a review batch, but
        # never constitutes consent to send an employment application.
        app.status = JobStatus.DRAFT
        app.approved_at = None
        app.approval_source = None
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
        app.profile_version = expected_profile_version
        db.flush()

        db_job.status = JobStatus.DRAFT

        from core.application_audit import record_application_event

        record_application_event(
            db,
            app.id,
            "application_generated",
            actor="worker",
            details={
                "selected_cv_id": app.selected_cv_id,
                "profile_version": app.profile_version,
                "state": "draft",
            },
        )

        db.commit()

        logger.info(
            "application_generated",
            job=db_job.title,
            score=db_job.score,
            threshold=settings.auto_apply_threshold,
            has_placeholders=generated.has_placeholders,
            auto_eligible=auto_eligible,
            reason=(
                "Eligible for explicit batch approval"
                if auto_eligible
                else "Score below threshold, draft-only, or CV routing review required"
            ),
        )

        # ── Notify originating WhatsApp sender (Cloud API) ────────────────
        # Only when draft (not auto-approved) and Cloud API is configured
        if settings.whatsapp_api_token and settings.whatsapp_phone_number_id:
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

                        run_async(
                            _send_approval_buttons(
                                sender,
                                job_id,
                                db_job.title,
                                db_job.company,
                                db_job.score or 0.0,
                                settings,
                            )
                        )
                        logger.info(
                            "whatsapp_approval_sent",
                            job=db_job.title,
                            sender=sender,
                        )
            except Exception as notify_err:
                logger.warning("whatsapp_notify_failed", error=str(notify_err))

        if auto_eligible:
            logger.info(
                "application_ready_for_batch_review",
                job=db_job.title,
                app_id=app.id,
            )

    except Exception as exc:
        db.rollback()
        logger.error("generation_failed", error=str(exc))
        raise self.retry(exc=exc, countdown=60)
    finally:
        db.close()


# ── Task 5: Submit application (only if approved) ─────────


@shared_task(name="worker.tasks.submit_application_task", bind=True, max_retries=1)
def submit_application_task(self, application_id: int):
    """Fail-closed compatibility handler for stale application-ID messages.

    PR2 commands are addressed by command ID in ``worker.submission_commands``.
    Reinterpreting an old application ID as a command ID could target the wrong
    external action, so this task intentionally performs no database mutation
    and invokes no adapter.
    """
    del self
    logger.warning(
        "legacy_submission_task_blocked",
        application_id=application_id,
        reason_code="DATABASE_COMMAND_REQUIRED",
    )
    return {
        "state": "blocked",
        "reason_code": "DATABASE_COMMAND_REQUIRED",
    }

    # Unreachable v3 implementation retained for one release as migration
    # context. It is removed after old broker messages have expired.
    from profile.loader import get_profile

    from jobs.models import JobData
    from submitters.ashby import AshbySubmitter
    from submitters.base import DraftOnlySubmitter, SubmissionResult
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
        if (
            not settings.dry_run
            and not settings.draft_only
            and not _validated_submit_command_available(db, application_id)
        ):
            app = db.get(Application, application_id)
            if app is not None and app.status == JobStatus.APPROVED:
                app.status = JobStatus.NEEDS_REVIEW
                app.needs_review_reason = "SUBMIT_PERMIT_REQUIRED"
                if app.job:
                    app.job.status = JobStatus.NEEDS_REVIEW
                from core.application_audit import record_application_event

                record_application_event(
                    db,
                    app.id,
                    "submission_dispatch_blocked",
                    actor="worker",
                    details={
                        "reason_code": "SUBMIT_PERMIT_REQUIRED",
                        "external_action_started": False,
                    },
                )
                db.commit()
            logger.warning(
                "submission_dispatch_blocked",
                application_id=application_id,
                reason_code="SUBMIT_PERMIT_REQUIRED",
            )
            return

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
            # Authenticated Workday browser session; final click remains
            # independently gated by PORTAL_FINAL_SUBMIT_ENABLED.
            WorkdaySubmitter(db=db),
        ]

        job_ref = JobData(
            title=db_job.title,
            company=db_job.company,
            location=db_job.location,
            apply_url=db_job.apply_url,
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
            db.query(UserProfileVersion).order_by(UserProfileVersion.version.desc()).first()
        )
        if app.selected_cv_id:
            from profile.cv_routing import load_routing_config

            try:
                routing_config = load_routing_config(settings.cv_routing_path)
                cv = next(
                    (item for item in routing_config.cvs if item.id == app.selected_cv_id),
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

        # Route to the matching submitter and preserve its authoritative result.
        result = None
        if settings.dry_run:
            result = SubmissionResult(
                success=True,
                platform="dry_run",
                status="draft_only",
                error="DRY_RUN",
                reason_code="DRY_RUN_DISCARDED",
            )
        elif settings.draft_only:
            result = run_async(
                DraftOnlySubmitter().submit(
                    job_ref,
                    generated,
                    profile_dict,
                    resume_path,
                )
            )
        else:
            from submitters.platforms import adapter_for_url, detect_platform

            route_url = job_ref.apply_url or job_ref.source_url
            descriptor = adapter_for_url(route_url)
            detected_platform = detect_platform(route_url)
            if descriptor is None or not descriptor.allows_live_submission:
                selector_version = (
                    descriptor.selector_version if descriptor is not None else "unregistered"
                )
                qualification = (
                    descriptor.qualification.value if descriptor is not None else "unregistered"
                )
                logger.warning(
                    "adapter_not_qualified",
                    platform=detected_platform,
                    adapter_version=(
                        descriptor.adapter_version if descriptor is not None else "unregistered"
                    ),
                    qualification=qualification,
                )
                result = SubmissionResult(
                    success=False,
                    platform=detected_platform,
                    status="failed",
                    error="NEEDS_REVIEW:ADAPTER_NOT_QUALIFIED",
                    reason_code="ADAPTER_NOT_QUALIFIED",
                    diagnostic_details={
                        "selector_version": selector_version,
                        "terminal_reason": "ADAPTER_NOT_QUALIFIED",
                    },
                )
            else:
                matching_submitter = next(
                    (
                        submitter
                        for submitter in all_submitters
                        if submitter.platform_name == descriptor.platform
                        and submitter.can_submit(job_ref)
                    ),
                    None,
                )
                if matching_submitter is None:
                    result = SubmissionResult(
                        success=False,
                        platform=descriptor.platform,
                        status="failed",
                        error="NEEDS_REVIEW:ADAPTER_ROUTE_MISMATCH",
                        reason_code="ADAPTER_ROUTE_MISMATCH",
                        diagnostic_details={
                            "selector_version": descriptor.selector_version,
                            "terminal_reason": "ADAPTER_ROUTE_MISMATCH",
                        },
                    )
                else:
                    if isinstance(matching_submitter, LinkedInV2Submitter):
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
                    result = run_async(
                        matching_submitter.submit(
                            job_ref,
                            generated,
                            profile_dict,
                            resume_path,
                        )
                    )
                    logger.info(
                        "submitter_attempt",
                        platform=matching_submitter.platform_name,
                        adapter_version=descriptor.adapter_version,
                        status=result.status,
                        success=result.success,
                    )

        # abort-don't-lie: a blocked required field is surfaced as
        # NEEDS_REVIEW rather than silently drafted or failed.
        #
        # Read this before unsupported-platform fallback so the operator sees
        # which required question stopped the adapter.
        needs_review_reason = None
        if result is not None and result.error and result.error.startswith("NEEDS_REVIEW:"):
            needs_review_reason = result.error.split("NEEDS_REVIEW:", 1)[1]

        # Fall back only when no adapter handled the platform. A handled
        # failure/unknown outcome is authoritative and must remain visible.
        if result is None:
            result = run_async(
                DraftOnlySubmitter().submit(job_ref, generated, profile_dict, resume_path)
            )

        # Finalize the pre-committed attempt. No external action can run before
        # this attempt exists, so task redelivery cannot duplicate the action.
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        from core.submission_truth import (
            EMPLOYER_VERIFIED_REASON_CODES,
            has_nonblank_employer_evidence,
        )
        from worker.submission_attempts import (
            classify_reason,
            redacted_result_diagnostics,
        )

        claimed_submitted = result.status == "submitted" and result.success
        result_reason_code = result.reason_code or classify_reason(
            result.error,
            result.status,
        )
        employer_verified = (
            claimed_submitted
            and result_reason_code in EMPLOYER_VERIFIED_REASON_CODES
            and has_nonblank_employer_evidence(
                result.confirmation_id,
                result.confirmation_url,
            )
        )
        diagnostic_details = dict(result.diagnostic_details)
        if employer_verified:
            sub_status = SubmissionStatus.SUCCESS
        elif claimed_submitted:
            # A click, redirect, generic success phrase, or adapter boolean is
            # not employer evidence. The irreversible action is indeterminate.
            sub_status = SubmissionStatus.UNKNOWN
            result_reason_code = "FINAL_ACTION_UNCONFIRMED"
            diagnostic_details["terminal_reason"] = "FINAL_ACTION_UNCONFIRMED"
        elif result.status == "draft_only":
            sub_status = SubmissionStatus.DRAFT_ONLY
        elif result.status == "unknown":
            sub_status = SubmissionStatus.UNKNOWN
        else:
            sub_status = SubmissionStatus.FAILED

        now = _dt.now(_UTC).replace(tzinfo=None)
        attempt.submitter_name = result.platform
        attempt.status = sub_status
        attempt.confirmation_url = result.confirmation_url
        attempt.confirmation_id = result.confirmation_id
        attempt.error_message = None
        attempt.reason_code = result_reason_code
        attempt.diagnostic_details = redacted_result_diagnostics(
            result.error,
            diagnostic_details,
        )
        attempt.finished_at = now
        attempt.submitted_at = now if employer_verified else None

        # Job/application status mirrors submission outcome.
        #
        # CRITICAL: app.status must leave APPROVED on every terminal branch.
        # Otherwise the drainer can select it again and create a new attempt.
        if employer_verified:
            db_job.status = JobStatus.SUBMITTED
            app.status = JobStatus.SUBMITTED
            if result.platform == "linkedin":
                governor.record_application()
                app.submission_channel = "linkedin_easy"
        elif claimed_submitted:
            app.needs_review_reason = (
                "Final action was reported without employer-verifiable evidence."
            )
            app.status = JobStatus.NEEDS_REVIEW
            db_job.status = JobStatus.NEEDS_REVIEW
        elif needs_review_reason is not None:
            app.needs_review_reason = needs_review_reason
            app.status = JobStatus.NEEDS_REVIEW
            db_job.status = JobStatus.NEEDS_REVIEW
        elif result.status == "unknown":
            app.needs_review_reason = "Submission outcome is unknown; reconcile manually."
            app.status = JobStatus.NEEDS_REVIEW
            db_job.status = JobStatus.NEEDS_REVIEW
        elif result.status in ("draft_only", "captcha_blocked"):
            db_job.status = JobStatus.DRAFT
            app.status = JobStatus.DRAFT
        else:
            db_job.status = JobStatus.FAILED
            app.status = JobStatus.FAILED

        from core.application_audit import record_application_event

        record_application_event(
            db,
            app.id,
            "submission_attempt_finished",
            actor="worker",
            details={
                "attempt_number": attempt.attempt_number,
                "platform": result.platform,
                "reason_code": attempt.reason_code,
                "selected_cv_id": attempt.selected_cv_id,
                "profile_version": attempt.profile_version,
                "state": attempt.status.value,
            },
        )
        db.commit()

        logger.info(
            "submission_completed",
            job=db_job.title,
            platform=result.platform,
            status=attempt.status.value,
            employer_verified=employer_verified,
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
