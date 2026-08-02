from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from job_control_plane.config import Settings
from job_control_plane.models import RunnerDevice
from job_control_plane.protocol import (
    AdapterCode,
    AdapterStatusSummary,
    AutomationPolicyState,
    AutomationPolicySummary,
    DiscoverySourceCode,
    DiscoverySourceSummary,
    HeartbeatEnvelope,
    HeartbeatPayload,
    PipelineCounters,
    QualificationTierCode,
    RunnerStatus,
    SourceHealth,
    operations_summary_digest,
)


def _summary(now: datetime) -> dict[str, object]:
    pipeline = PipelineCounters(
        discovered=123,
        source_occurrences=147,
        deduplicated=24,
        eligible=17,
        prepared=11,
        quarantined=6,
        employer_confirmed=3,
    )
    policy = AutomationPolicySummary(
        state=AutomationPolicyState.ACTIVE,
        revision=4,
        expires_at=now + timedelta(days=7),
        daily_remaining=21,
        hourly_remaining=4,
        kill_switch_active=False,
    )
    sources = (
        DiscoverySourceSummary(
            source=DiscoverySourceCode.GREENHOUSE,
            status=SourceHealth.HEALTHY,
            enabled_count=2,
            source_count=2,
        ),
    )
    adapters = (
        AdapterStatusSummary(
            adapter=AdapterCode.GREENHOUSE,
            qualification_tier=QualificationTierCode.FIXTURE_QUALIFIED,
            final_execution_enabled=False,
            qualified_form_scope_count=0,
        ),
    )
    return {
        "pipeline": pipeline,
        "policy": policy,
        "sources": sources,
        "adapters": adapters,
        "operations_digest": operations_summary_digest(
            pipeline=pipeline,
            policy=policy,
            sources=sources,
            adapters=adapters,
        ),
    }


def test_heartbeat_summary_is_canonical_bounded_and_rejects_tampering() -> None:
    now = datetime.now(UTC)
    summary = _summary(now)
    payload = HeartbeatPayload(
        boot_id=uuid4(),
        release_digest="a" * 64,
        status=RunnerStatus.READY,
        **summary,
    )

    assert set(payload.model_dump(mode="json")) == {
        "boot_id",
        "release_digest",
        "status",
        "pipeline",
        "policy",
        "sources",
        "adapters",
        "operations_digest",
    }
    serialized = payload.model_dump_json()
    for forbidden in (
        "email",
        "phone",
        "candidate",
        "job_url",
        "job_title",
        "company",
        "answer",
        "cv_hash",
        "tenant",
    ):
        assert forbidden not in serialized

    with pytest.raises(ValidationError, match="operations summary digest mismatch"):
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="b" * 64,
            status=RunnerStatus.READY,
            **{**summary, "operations_digest": "0" * 64},
        )

    duplicate_sources = (
        summary["sources"][0],
        summary["sources"][0],
    )
    with pytest.raises(ValidationError, match="source summaries must be unique and sorted"):
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="c" * 64,
            status=RunnerStatus.READY,
            pipeline=summary["pipeline"],
            policy=summary["policy"],
            sources=duplicate_sources,
            adapters=summary["adapters"],
            operations_digest=operations_summary_digest(
                pipeline=summary["pipeline"],
                policy=summary["policy"],
                sources=duplicate_sources,
                adapters=summary["adapters"],
            ),
        )


def test_signed_summary_persists_and_renders_only_redacted_operations(
    client: TestClient,
    settings: Settings,
    sign_runner: Callable[..., Any],
    authenticated: str,
) -> None:
    now = datetime.now(UTC)
    summary = _summary(now)
    heartbeat = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="d" * 64,
            status=RunnerStatus.READY,
            **summary,
        ),
        issued_at=now,
    )
    response = client.post(
        "/api/runner/heartbeat",
        json=heartbeat.model_dump(mode="json"),
    )
    assert response.status_code == 200

    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as db:
            device = db.get(RunnerDevice, str(settings.runner_device_id))
            assert device is not None
            assert device.operations_digest == summary["operations_digest"]
            assert device.policy_status == "active"
            assert device.policy_revision == 4
            assert device.policy_daily_remaining == 21
            assert device.policy_hourly_remaining == 4
            assert device.kill_switch_active is False
            assert '"discovered":123' in device.pipeline_counters_json
            assert '"source":"greenhouse"' in device.source_status_json
            assert '"adapter":"greenhouse"' in device.adapter_status_json
    finally:
        engine.dispose()

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Private runner" in dashboard.text
    assert "Pipeline counters" in dashboard.text
    assert "Operations summary: verified" in dashboard.text
    assert summary["operations_digest"] in dashboard.text
    assert "Auto-eligible" in dashboard.text
    assert ">17<" in dashboard.text
    assert "Discovery source codes" in dashboard.text
    assert "greenhouse" in dashboard.text
    assert "fixture_qualified" in dashboard.text
    assert "Daily remaining 21" in dashboard.text
    for forbidden in (
        "candidate@example.com",
        "private.example",
        "job_url",
        "cv_hash",
        "question_text",
        "answer_text",
    ):
        assert forbidden not in dashboard.text


def test_legacy_heartbeat_clears_stale_operations_summary(
    client: TestClient,
    settings: Settings,
    sign_runner: Callable[..., Any],
) -> None:
    now = datetime.now(UTC)
    enriched = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="e" * 64,
            status=RunnerStatus.READY,
            **_summary(now),
        ),
        issued_at=now,
    )
    assert (
        client.post("/api/runner/heartbeat", json=enriched.model_dump(mode="json")).status_code
        == 200
    )
    legacy = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=uuid4(),
            release_digest="f" * 64,
            status=RunnerStatus.DEGRADED,
        ),
        issued_at=now + timedelta(seconds=1),
    )
    assert (
        client.post("/api/runner/heartbeat", json=legacy.model_dump(mode="json")).status_code == 200
    )

    engine = create_engine(settings.database_url)
    try:
        with Session(engine) as db:
            device = db.get(RunnerDevice, str(settings.runner_device_id))
            assert device is not None
            assert device.operations_digest is None
            assert device.policy_status == "unavailable"
            assert device.pipeline_counters_json == "{}"
            assert device.source_status_json == "[]"
            assert device.adapter_status_json == "[]"
    finally:
        engine.dispose()
