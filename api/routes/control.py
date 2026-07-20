"""Kill switch + governor status routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from core.governor import get_governor
from db.session import get_db

router = APIRouter(prefix="/api/control", tags=["control"])


@router.post("/kill")
async def kill():
    get_governor().kill()
    return {"status": "killed"}


@router.post("/resume")
async def resume():
    get_governor().resume()
    return {"status": "resumed"}


@router.get("/status")
async def status():
    return get_governor().status()


@router.get("/overview")
async def overview(db: Session = Depends(get_db)):
    from datetime import datetime

    from db.models import Application, Job, JobStatus
    from worker.digest import build_digest

    gov = get_governor().status()
    summary = build_digest(db, datetime.utcnow().date())
    rows = (db.query(Application, Job)
              .join(Job, Application.job_id == Job.id)
              .filter(Application.status == JobStatus.NEEDS_REVIEW)
              .limit(50).all())
    needs = [{"job_id": j.id, "title": j.title, "reason": a.needs_review_reason}
             for a, j in rows]
    return {"governor": gov,
            "counts": {"applied": summary.applied, "needs_review": summary.needs_review,
                       "failed": summary.failed, "outbound_sent": summary.outbound_sent},
            "needs_review": needs}
