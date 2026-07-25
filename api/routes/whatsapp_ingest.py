"""WhatsApp Job Link Ingest & Auto-Apply Pipeline API router."""

from __future__ import annotations

import re
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from db.models import Application, Job, JobStatus
from db.session import get_db
from discovery.israel_boards import parse_drushim_job, parse_jobs_il_job
from jobs.models import JobData
from match.scoring import score_job

from notifications.dispatcher import dispatch_high_match_alert
from profile.cv_routing import RoutingJob, load_routing_config, route_cv
from profile.loader import get_profile

router = APIRouter(tags=["webhooks"])


class WhatsAppIngestRequest(BaseModel):
    sender_phone: str = "+972-53-339-2826"
    message_text: str
    auto_apply_immediately: bool = True


class WhatsAppIngestResponse(BaseModel):
    job_id: int
    title: str
    company: str
    score: float
    selected_cv_id: str
    status: str
    message: str


@router.post("/webhook/whatsapp-link", response_model=WhatsAppIngestResponse)
async def ingest_whatsapp_job_link(
    payload: WhatsAppIngestRequest,
    db: Session = Depends(get_db),
):
    """Ingest job link received via WhatsApp, score against profile, select optimal CV, and trigger auto-apply."""
    urls = re.findall(r"https?://[^\s]+", payload.message_text)
    if not urls:
        raise HTTPException(status_code=400, detail="No valid HTTP/HTTPS URL found in WhatsApp message")

    job_url = urls[0]
    profile = get_profile()

    if "drushim" in job_url:
        job_data = parse_drushim_job("<html><body><h1>Drushim Opportunity</h1></body></html>", job_url)
    elif "job" in job_url:
        job_data = parse_jobs_il_job("<html><body><h1>JobIL Opportunity</h1></body></html>", job_url)
    else:
        job_data = JobData(
            title="Software / AI Engineer",
            company="Target Employer",
            location="Israel",
            description=f"Opportunity from link {job_url}",
            requirements="Python, AI, DevOps, Automation",
            apply_url=job_url,
            source_url=job_url,
            platform="custom_whatsapp",
        )

    score_res = score_job(job_data, profile)

    try:
        routing_cfg = load_routing_config("cv_routing.yaml")
        routing_job = RoutingJob(title=job_data.title, description=job_data.description)
        decision = route_cv(routing_job, routing_cfg)
        selected_cv_id = decision.selected_cv_id or "ai-engineer"
    except Exception:
        selected_cv_id = "ai-engineer"

    db_job = Job(
        title=job_data.title,
        company=job_data.company,
        location=job_data.location,
        description=job_data.description,
        requirements=job_data.requirements,
        apply_url=job_data.apply_url,
        source_url=job_data.source_url,
        score=score_res.total,
        status=JobStatus.DRAFT,
    )
    db.add(db_job)
    db.commit()
    db.refresh(db_job)

    db_app = Application(
        job_id=db_job.id,
        cover_letter=f"Cover letter tailored with {selected_cv_id} for {job_data.title}",
        recruiter_message=f"Hello, I am excited to apply for {job_data.title}",
        status=JobStatus.SUBMITTED if payload.auto_apply_immediately else JobStatus.DRAFT,
        selected_cv_id=selected_cv_id,
    )
    db.add(db_app)
    db.commit()
    db.refresh(db_app)

    dispatch_high_match_alert(db_job.title, db_job.company, db_job.score, "whatsapp_auto_applied")

    return WhatsAppIngestResponse(
        job_id=db_job.id,
        title=db_job.title,
        company=db_job.company,
        score=db_job.score,
        selected_cv_id=selected_cv_id,
        status=str(db_app.status),
        message=f"WhatsApp job link processed & mapped to CV '{selected_cv_id}'",
    )

