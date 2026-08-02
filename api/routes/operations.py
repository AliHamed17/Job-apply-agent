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


class DiscoverySourceRow(BaseModel):
    source_type: str = Field(max_length=32)
    status: str = Field(max_length=16)
    source_count: int = Field(ge=0)
    enabled_count: int = Field(ge=0)
    cadence_seconds: int = Field(ge=0, le=86_400)
    next_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=64)


class PipelineCounts(BaseModel):
    discovered: int = Field(ge=0)
    source_occurrences: int = Field(ge=0)
    deduplicated: int = Field(ge=0)
    eligible: int = Field(ge=0)
    prepared: int = Field(ge=0)
    quarantined: int = Field(ge=0)
    employer_confirmed: int = Field(ge=0)


class RoleCVMatrixRow(BaseModel):
    cv_route: str = Field(max_length=64)
    total: int = Field(ge=0)
    eligible: int = Field(ge=0)
    needs_review: int = Field(ge=0)
    excluded: int = Field(ge=0)
    average_fit_score: float = Field(ge=0, le=100)
    average_routing_confidence: float = Field(ge=0, le=1)


class FitEvidenceRow(BaseModel):
    factor: str = Field(max_length=32)
    result: str = Field(max_length=16)
    reason_codes: list[str] = Field(max_length=12)


class RecentFitDecisionRow(BaseModel):
    decision_id: int = Field(ge=1)
    job_id: int = Field(ge=1)
    cv_route: str = Field(max_length=64)
    fit_score: float = Field(ge=0, le=100)
    routing_confidence: float = Field(ge=0, le=1)
    routing_margin: float = Field(ge=0, le=1)
    disposition: str = Field(max_length=24)
    quality_eligible: bool
    fallback_reason: str | None = Field(default=None, max_length=64)
    hard_exclusions: list[str] = Field(max_length=20)
    uncertainty: list[str] = Field(max_length=30)
    unsupported_required_skill_count: int = Field(ge=0, le=100)
    evidence: list[FitEvidenceRow] = Field(min_length=7, max_length=7)
    created_at: datetime


class AutomationPolicyRow(BaseModel):
    active: bool
    reason_code: str | None = Field(default=None, max_length=64)
    revision: int = Field(ge=0)
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    minimum_fit_score: float | None = Field(default=None, ge=85, le=100)
    daily_limit: int = Field(ge=0, le=25)
    daily_remaining: int = Field(ge=0, le=25)
    hourly_limit: int = Field(ge=0, le=5)
    hourly_remaining: int = Field(ge=0, le=5)
    company_limit: int = Field(ge=0, le=2)
    company_window_days: int = Field(ge=0, le=14)
    permitted_adapters: list[str] = Field(max_length=16)
    geographies: list[str] = Field(max_length=8)
    role_family_count: int = Field(ge=0, le=100)
    qualified_form_contract_count: int = Field(ge=0)
    kill_switch_active: bool
    kill_switch_revision: int = Field(ge=0)


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


class RecentAttemptRow(BaseModel):
    attempt_id: int = Field(ge=1)
    application_id: int = Field(ge=1)
    attempt_number: int = Field(ge=1)
    stage: str = Field(max_length=24)
    outcome: str = Field(max_length=32)
    reason_code: str = Field(max_length=64)
    ats: str = Field(max_length=32)
    adapter_version: str = Field(max_length=32)
    selector_version: str = Field(max_length=64)
    cv_route: str = Field(max_length=64)
    form_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    attachment_verified: bool
    verification_kind: str = Field(max_length=48)
    evidence_digest: str | None = Field(default=None, min_length=64, max_length=64)
    authority_kind: str = Field(max_length=32)
    created_at: datetime
    started_at: datetime | None = None
    final_action_at: datetime | None = None
    finished_at: datetime | None = None


class OperationsDashboardResponse(BaseModel):
    generated_at: datetime
    window_days: int = Field(ge=1, le=90)
    dependencies: list[DependencyStatus] = Field(max_length=16)
    last_successful_discovery: LastSuccessfulDiscovery | None
    discovery_sources: list[DiscoverySourceRow] = Field(max_length=100)
    pipeline_counts: PipelineCounts
    role_cv_matrix: list[RoleCVMatrixRow] = Field(max_length=100)
    recent_fit_decisions: list[RecentFitDecisionRow] = Field(max_length=25)
    automation_policy: AutomationPolicyRow
    adapter_matrix: list[AdapterMatrixRow] = Field(max_length=100)
    failure_clusters: list[FailureClusterRow] = Field(max_length=100)
    queue_depth: list[QueueDepthRow] = Field(max_length=32)
    attempt_stages: list[AttemptStageRow] = Field(max_length=100)
    attempt_outcomes: list[AttemptOutcomeRow] = Field(max_length=100)
    form_resolution: list[FormResolutionRow] = Field(max_length=100)
    attachment_results: list[AttachmentResultRow] = Field(max_length=100)
    evidence_types: list[EvidenceTypeRow] = Field(max_length=100)
    recent_attempts: list[RecentAttemptRow] = Field(max_length=25)
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
