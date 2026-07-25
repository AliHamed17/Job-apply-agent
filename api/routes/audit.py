"""System audit log API router."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from core.audit import get_audit_logs

router = APIRouter(tags=["audit"])


class AuditLogResponse(BaseModel):
    total: int
    logs: list[dict] = Field(default_factory=list)


@router.get("/audit/logs", response_model=AuditLogResponse)
async def list_system_audit_logs(
    level: str | None = None,
    limit: int = 50,
):
    """Retrieve structured system audit events."""
    logs = get_audit_logs(level=level, limit=limit)
    return AuditLogResponse(
        total=len(logs),
        logs=logs,
    )
