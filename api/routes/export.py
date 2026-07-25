"""Application batch export API router (CSV / JSON)."""

from __future__ import annotations

import csv
import io
from typing import Literal

from fastapi import APIRouter, Depends, Response
from sqlalchemy.orm import Session

from db.models import Application
from db.session import get_db

router = APIRouter(tags=["export"])


@router.get("/export/applications")
async def export_applications(
    format: Literal["json", "csv"] = "json",
    db: Session = Depends(get_db),
):
    """Export application records in JSON or CSV format for spreadsheet / Notion export."""
    apps = (
        db.query(Application)
        .order_by(Application.created_at.desc())
        .all()
    )

    records = [
        {
            "application_id": a.id,
            "job_id": a.job_id,
            "title": a.job.title if a.job else "",
            "company": a.job.company if a.job else "",
            "location": a.job.location if a.job else "",
            "score": a.job.score if a.job else None,
            "status": a.status.value if hasattr(a.status, "value") else str(a.status),
            "selected_cv_id": a.selected_cv_id,
            "apply_url": a.job.apply_url if a.job else "",
            "created_at": str(a.created_at),
        }
        for a in apps
    ]

    if format == "json":
        return records

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "application_id", "job_id", "title", "company", "location",
            "score", "status", "selected_cv_id", "apply_url", "created_at",
        ],
    )
    writer.writeheader()
    writer.writerows(records)

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=applications_export.csv"},
    )
