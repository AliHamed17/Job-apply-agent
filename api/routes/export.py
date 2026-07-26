"""Application batch export API router (CSV / JSON)."""

from __future__ import annotations

import csv
import io
from typing import Literal

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from api.submission_display import job_submission_display
from core.submission_truth import is_employer_verified
from db.models import Application
from db.session import get_db

router = APIRouter(tags=["export"])


@router.get("/export/applications")
async def export_applications(
    format: Literal["json", "csv"] = "json",
    db: Session = Depends(get_db),
):
    """Export application records in JSON or CSV format for spreadsheet / Notion export."""
    apps = db.query(Application).order_by(Application.created_at.desc()).all()

    records = []
    for application in apps:
        job_display = (
            job_submission_display(application.job) if application.job is not None else None
        )
        employer_verified = is_employer_verified(application.submission)
        source_status = (
            application.status.value
            if hasattr(application.status, "value")
            else str(application.status)
        )
        display_status = (
            "submitted"
            if employer_verified
            else ("unverified" if source_status == "submitted" else source_status)
        )
        records.append(
            {
                "application_id": application.id,
                "job_id": application.job_id,
                "title": application.job.title if application.job else "",
                "company": application.job.company if application.job else "",
                "location": application.job.location if application.job else "",
                "score": application.job.score if application.job else None,
                "status": display_status,
                "source_status": source_status,
                "employer_verified": employer_verified,
                "job_display_status": (
                    job_display.display_status if job_display is not None else ""
                ),
                "selected_cv_id": application.selected_cv_id,
                "apply_url": application.job.apply_url if application.job else "",
                "created_at": str(application.created_at),
            }
        )

    if format == "json":
        return records

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "application_id",
            "job_id",
            "title",
            "company",
            "location",
            "score",
            "status",
            "source_status",
            "employer_verified",
            "job_display_status",
            "selected_cv_id",
            "apply_url",
            "created_at",
        ],
    )
    writer.writeheader()
    writer.writerows(records)

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=applications_export.csv"},
    )
