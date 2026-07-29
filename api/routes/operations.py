"""Protected, privacy-bounded operational evidence endpoint."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from core.config import get_settings
from core.dashboard_operations import build_operations_snapshot
from core.operations import readiness_report
from db.session import get_db

router = APIRouter(tags=["operations"])


class DependencyStatus(BaseModel):
    name: str = Field(max_length=32)
    ok: bool
    status: str = Field(max_length=16)
    reason_code: str = Field(max_length=64)
    age_seconds: float | None = Field(default=None, ge=0)
    last_seen_at: datetime | None = None


class LastSuccessfulDiscovery(BaseModel):
    finished_at: datetime


class AdapterMatrixRow(BaseModel):
    ats: str = Field(max_length=32)
    adapter_version: str = Field(max_length=32)
    selector_version: str = Field(max_length=64)
    qualification_tier: str = Field(max_length=32)
    final_execution_enabled: bool
    qualified_form_scope_count: int = Field(ge=0)


class QueueDepthRow(BaseModel):
    queue: str = Field(max_length=48)
    count: int = Field(ge=0)


class AttemptStageRow(BaseModel):
    stage: str = Field(max_length=24)
    ats: str = Field(max_length=32)
    adapter_version: str = Field(max_length=32)
    selector_version: str = Field(max_length=64)
    count: int = Field(ge=0)


class AttemptOutcomeRow(BaseModel):
    outcome: str = Field(max_length=32)
    reason_code: str = Field(max_length=64)
    ats: str = Field(max_length=32)
    adapter_version: str = Field(max_length=32)
    selector_version: str = Field(max_length=64)
    count: int = Field(ge=0)


class FailureClusterRow(BaseModel):
    reason_code: str = Field(max_length=64)
    ats: str = Field(max_length=32)
    adapter_version: str = Field(max_length=32)
    selector_version: str = Field(max_length=64)
    count: int = Field(ge=0)
    last_seen_at: datetime | None = None


class FormResolutionRow(BaseModel):
    resolver: str = Field(max_length=40)
    field_type: str = Field(max_length=24)
    reason_code: str = Field(max_length=64)
    ats: str = Field(max_length=32)
    adapter_version: str = Field(max_length=32)
    selector_version: str = Field(max_length=64)
    count: int = Field(ge=0)


class AttachmentResultRow(BaseModel):
    attachment_result: str = Field(max_length=24)
    result: str = Field(max_length=24)
    reason_code: str = Field(max_length=64)
    ats: str = Field(max_length=32)
    adapter_version: str = Field(max_length=32)
    selector_version: str = Field(max_length=64)
    count: int = Field(ge=0)


class EvidenceTypeRow(BaseModel):
    evidence_type: str = Field(max_length=48)
    ats: str = Field(max_length=32)
    adapter_version: str = Field(max_length=32)
    verification: str = Field(max_length=32)
    count: int = Field(ge=0)


class RuntimeIdentityRow(BaseModel):
    build_sha: str = Field(max_length=128)
    source_digest: str = Field(max_length=128)
    ui_asset_digest: str = Field(max_length=128)
    protocol_version: str = Field(max_length=128)
    boot_id: str = Field(max_length=128)
    runner_release: str = Field(max_length=128)
    started_at: datetime


class OperationsDashboardResponse(BaseModel):
    generated_at: datetime
    window_days: int = Field(ge=1, le=90)
    dependencies: list[DependencyStatus] = Field(max_length=16)
    last_successful_discovery: LastSuccessfulDiscovery | None
    adapter_matrix: list[AdapterMatrixRow] = Field(max_length=100)
    failure_clusters: list[FailureClusterRow] = Field(max_length=100)
    queue_depth: list[QueueDepthRow] = Field(max_length=32)
    attempt_stages: list[AttemptStageRow] = Field(max_length=100)
    attempt_outcomes: list[AttemptOutcomeRow] = Field(max_length=100)
    form_resolution: list[FormResolutionRow] = Field(max_length=100)
    attachment_results: list[AttachmentResultRow] = Field(max_length=100)
    evidence_types: list[EvidenceTypeRow] = Field(max_length=100)
    runtime_identity: RuntimeIdentityRow


@router.get(
    "/dashboard/operations",
    response_model=OperationsDashboardResponse,
)
async def dashboard_operations(
    window_days: int = Query(default=30, ge=1, le=90),
    db: Session = Depends(get_db),
):
    readiness = await run_in_threadpool(readiness_report, get_settings())
    return build_operations_snapshot(
        db,
        readiness,
        window_days=window_days,
    )
