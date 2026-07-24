"""WhatsApp/email outbound applier orchestration (Task 5.5).

Consumes a text-only WhatsApp post (a recruiter "hiring" broadcast with no
job-board URL), parses it into a job, scores it against the profile, and — if
it clears the bar — messages the recruiter back on whichever channel they
posted a contact for (WhatsApp DM preferred, email fallback), attaching the
CV. Gated by score, a governor-enforced daily send cap, and per-contact
dedup so we never spam or exceed budget.

All IO/LLM calls are injected via ``deps`` (a ``SimpleNamespace`` bundling
``parse``, ``bridge``, ``email``, ``gen_msg`` and ``now``) so tests can run
fully offline against fakes + an in-memory DB.
"""

from __future__ import annotations

import structlog

from ingestion.text_post_parser import ParsedPost, looks_like_job  # noqa: F401 (re-exported)
from jobs.models import JobData
from match.scoring import Action, decide_action, score_job
from worker.outbound_dedup import can_contact, record_contact

logger = structlog.get_logger(__name__)

__all__ = ["ParsedPost", "looks_like_job", "process_text_post"]


async def process_text_post(text, db, settings, profile, governor, deps, sender=None) -> str:
    """Parse, score, and (maybe) reply to a text-only job post.

    ``sender`` is the poster's own WhatsApp number (supplied by the bridge for
    group posts). It's used as the contact of last resort for "DM me" style
    posts that carry no phone/email in the body.

    Returns one of: "not_job" | "low_score" | "duplicate" | "capped" |
    "draft_only" | "sent_whatsapp" | "sent_email" | "no_contact".
    """
    parsed = await deps.parse(text)
    if not parsed.is_job:
        return "not_job"

    job = JobData(
        title=parsed.title,
        company=parsed.company,
        description=parsed.description,
    )

    breakdown = score_job(job, profile)
    action = decide_action(
        score=breakdown.total,
        auto_apply_enabled=settings.auto_apply,
        draft_only=settings.draft_only,
        skip_reason=breakdown.skip_reason,
        min_apply_score=settings.min_apply_score,
    )

    if action != Action.AUTO_APPLY and breakdown.total < settings.min_apply_score:
        logger.info("outbound_low_score", title=job.title, score=breakdown.total)
        return "low_score"

    ok, reason = governor.can_act()
    if not ok or governor.wa_remaining() <= 0:
        logger.info("outbound_capped", title=job.title, reason=reason)
        return "capped"

    if parsed.contact_phone:
        channel = "whatsapp"
        contact_value = parsed.contact_phone
    elif parsed.contact_email:
        channel = "email"
        contact_value = parsed.contact_email
    elif sender:
        # No phone/email in the post body — fall back to the poster's own
        # WhatsApp number (e.g. "interested? DM me"). This is the only
        # usable contact for such posts.
        channel = "whatsapp"
        contact_value = sender
    else:
        return "no_contact"

    now = deps.now
    if not can_contact(db, contact_value, settings.wa_contact_dedup_days, now):
        logger.info("outbound_duplicate", contact=contact_value)
        return "duplicate"

    # DRAFT_ONLY is the master switch — never DM/email a recruiter on the
    # user's behalf while it's on. Gated here (not earlier) so scoring,
    # the governor cap, and dedup all still run and log normally; only
    # the actual send (and its bookkeeping) is skipped.
    if settings.draft_only:
        logger.info("outbound_draft_only", title=job.title, contact=contact_value)
        return "draft_only"

    message = await deps.gen_msg(job, profile)
    pdf_path = profile.resume.pdf_path or None

    if channel == "whatsapp":
        success = await deps.bridge(contact_value, message, pdf_path, settings)
    else:
        subject = f"Application for {job.title}" if job.title else "Job application"
        success = await deps.email(contact_value, subject, message, pdf_path, settings)

    if not success:
        logger.warning("outbound_send_failed", channel=channel, contact=contact_value)
        return "no_contact"

    record_contact(
        db, contact_value,
        "whatsapp_dm" if channel == "whatsapp" else "email",
        job_id=None, now=now,
    )
    governor.wa_record()
    logger.info("outbound_sent", channel=channel, contact=contact_value, title=job.title)
    return "sent_whatsapp" if channel == "whatsapp" else "sent_email"
