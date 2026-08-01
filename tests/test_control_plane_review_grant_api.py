"""HTTP contract tests for the local-only remote-send review action."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api import main as api_main
from api.routes import applications as applications_route
from core.adapter_qualification_service import fixture_evidence_digest
from core.automation_policy_service import form_contract_digest
from core.config import Settings
from core.runtime_identity import get_runtime_identity
from db.models import (
    AdapterQualificationRecord,
    Application,
    Base,
    ControlPlaneReviewGrant,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
)
from ingestion.url_utils import normalize_url, url_hash
from submitters.platforms import (
    TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    QualificationTier,
    adapter_for_url,
)


@pytest.fixture
def review_grant_api(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control-plane-review-grant-api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    now = datetime.now(UTC).replace(tzinfo=None)
    fingerprint = "f" * 64
    job = Job(
        title="Private Candidate Role",
        company="Private Employer",
        source_url="https://boards.greenhouse.io/acme/jobs/123",
        apply_url="https://boards.greenhouse.io/acme/jobs/123",
        status=JobStatus.DRAFT,
    )
    application = Application(
        job=job,
        status=JobStatus.DRAFT,
        selected_cv_id="private-cv.pdf",
        selected_cv_hash="c" * 64,
        profile_version=1,
        revision=7,
        prepared_revision=7,
        approved_at=now,
        approval_source="manual_prepare",
    )
    db.add(application)
    db.flush()
    descriptor = adapter_for_url(job.apply_url)
    assert descriptor is not None
    plan = FormPlan(
        plan_id=str(uuid4()),
        application_id=application.id,
        application_revision=application.revision,
        adapter_name=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        selector_version=descriptor.selector_version,
        fingerprint=fingerprint,
        selected_cv_id=application.selected_cv_id,
        selected_cv_hash=application.selected_cv_hash,
        attached_cv_id=application.selected_cv_id,
        attached_cv_hash=application.selected_cv_hash,
        attachment_verified=True,
        profile_version=application.profile_version,
        fields_json="[]",
        disclosures_json="[]",
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.flush()
    db.add(
        AdapterQualificationRecord(
            qualification_tier="live_canary_qualified",
            adapter_name=descriptor.platform,
            adapter_version=descriptor.adapter_version,
            selector_version=descriptor.selector_version,
            execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
            form_fingerprint=fingerprint,
            form_contract_digest=form_contract_digest(plan),
            fixture_digest=fixture_evidence_digest(descriptor.platform),
            application_id=application.id,
            application_revision=application.revision,
            form_plan_id=plan.id,
            attempt_id=900_002,
            job_url_hash=url_hash(normalize_url(job.apply_url)),
            evidence_digest="9" * 64,
            runner_release=get_runtime_identity().release_id,
            qualified_at=now,
        )
    )
    db.commit()
    reviewed = {
        "application_id": application.id,
        "application_revision": application.revision,
        "plan_id": plan.plan_id,
        "fingerprint": fingerprint,
    }
    db.close()

    settings = Settings(
        _env_file=None,
        app_env="test",
        secret_key="control-plane-operator-test-secret-" + "x" * 32,
        dry_run=False,
        draft_only=False,
        auto_apply=False,
        portal_final_submit_enabled=True,
        live_automation_acknowledged=True,
    )
    live_descriptor = replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(fingerprint,),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
    )

    def override_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(api_main, "settings", settings)
    monkeypatch.setattr(api_main, "rate_limit_allowed", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(applications_route, "get_settings", lambda: settings)
    monkeypatch.setattr(applications_route, "readiness_report", lambda _settings: {})
    monkeypatch.setattr(
        applications_route,
        "build_runtime_capabilities",
        lambda *_args, **_kwargs: {"submission": {"allowed": True, "reasons": []}},
    )
    monkeypatch.setattr(
        applications_route,
        "adapter_for_url",
        lambda _url: live_descriptor,
    )
    api_main.app.dependency_overrides[applications_route.get_db] = override_db
    client = TestClient(api_main.app)
    try:
        yield client, factory, reviewed, settings, descriptor
    finally:
        api_main.app.dependency_overrides.pop(applications_route.get_db, None)
        client.close()
        engine.dispose()


def _payload(reviewed, **updates):
    payload = {
        "acknowledgement": "ALLOW_REMOTE_SEND",
        "application_revision": reviewed["application_revision"],
        "form_plan_id": reviewed["plan_id"],
    }
    payload.update(updates)
    return payload


def _assert_no_external_action(factory):
    db = factory()
    try:
        assert db.query(Submission).count() == 0
        assert db.query(SubmissionCommand).count() == 0
    finally:
        db.close()


def test_review_grant_requires_strong_bearer_and_exact_acknowledgement(
    review_grant_api,
):
    client, factory, reviewed, settings, _descriptor = review_grant_api
    url = f"/api/applications/{reviewed['application_id']}/control-plane-review-grant"

    assert client.post(url, json=_payload(reviewed)).status_code == 401
    assert (
        client.post(
            url,
            json=_payload(reviewed),
            headers={"Authorization": "Bearer wrong-operator-secret"},
        ).status_code
        == 403
    )
    invalid_ack = client.post(
        url,
        json=_payload(reviewed, acknowledgement="SEND_APPLICATION"),
        headers={"Authorization": f"Bearer {settings.secret_key}"},
    )
    assert invalid_ack.status_code == 422
    db = factory()
    try:
        assert db.query(ControlPlaneReviewGrant).count() == 0
    finally:
        db.close()
    _assert_no_external_action(factory)


def test_review_grant_rejects_placeholder_operator_secret(
    review_grant_api,
    monkeypatch,
):
    client, factory, reviewed, _settings, _descriptor = review_grant_api
    weak_settings = Settings(
        _env_file=None,
        app_env="test",
        secret_key="change-me",
        dry_run=False,
        draft_only=False,
        portal_final_submit_enabled=True,
        live_automation_acknowledged=True,
    )
    monkeypatch.setattr(api_main, "settings", weak_settings)
    monkeypatch.setattr(applications_route, "get_settings", lambda: weak_settings)
    response = client.post(
        f"/api/applications/{reviewed['application_id']}/control-plane-review-grant",
        json=_payload(reviewed),
        headers={"Authorization": "Bearer change-me"},
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OPERATOR_AUTH_REQUIRED"
    _assert_no_external_action(factory)


@pytest.mark.parametrize(
    ("updates", "status_code", "reason_code"),
    [
        (
            {"application_revision": 8},
            409,
            "APPLICATION_REVISION_CHANGED",
        ),
        (
            {"form_plan_id": "00000000-0000-4000-8000-000000000000"},
            404,
            None,
        ),
    ],
)
def test_review_grant_requires_exact_revision_and_plan_binding(
    review_grant_api,
    updates,
    status_code,
    reason_code,
):
    client, factory, reviewed, settings, _descriptor = review_grant_api
    response = client.post(
        f"/api/applications/{reviewed['application_id']}/control-plane-review-grant",
        json=_payload(reviewed, **updates),
        headers={"Authorization": f"Bearer {settings.secret_key}"},
    )

    assert response.status_code == status_code
    if reason_code is not None:
        assert response.json()["detail"]["code"] == reason_code
    db = factory()
    try:
        assert db.query(ControlPlaneReviewGrant).count() == 0
    finally:
        db.close()
    _assert_no_external_action(factory)


def test_review_grant_mints_only_a_pending_projection(review_grant_api):
    client, factory, reviewed, settings, _descriptor = review_grant_api
    response = client.post(
        f"/api/applications/{reviewed['application_id']}/control-plane-review-grant",
        json=_payload(reviewed),
        headers={"Authorization": f"Bearer {settings.secret_key}"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["application_id"] == reviewed["application_id"]
    assert body["application_revision"] == reviewed["application_revision"]
    assert body["projection_state"] == "pending"
    assert set(body) == {
        "application_id",
        "application_ref",
        "grant_id",
        "application_revision",
        "adapter",
        "adapter_version",
        "expires_at",
        "projection_state",
    }
    db = factory()
    try:
        grant = db.query(ControlPlaneReviewGrant).one()
        assert grant.grant_ref == body["grant_id"]
        assert grant.projection_state == "pending"
        assert grant.consumed_at is None
        assert grant.projected_at is None
    finally:
        db.close()
    _assert_no_external_action(factory)


def test_review_grant_fails_closed_when_runtime_or_adapter_is_unqualified(
    review_grant_api,
    monkeypatch,
):
    client, factory, reviewed, settings, descriptor = review_grant_api
    url = f"/api/applications/{reviewed['application_id']}/control-plane-review-grant"
    headers = {"Authorization": f"Bearer {settings.secret_key}"}
    monkeypatch.setattr(
        applications_route,
        "build_runtime_capabilities",
        lambda *_args, **_kwargs: {"submission": {"allowed": False, "reasons": ["RUNNER_OFFLINE"]}},
    )
    runtime_response = client.post(url, json=_payload(reviewed), headers=headers)
    assert runtime_response.status_code == 409
    assert runtime_response.json()["detail"]["code"] == "RUNTIME_NOT_READY"

    monkeypatch.setattr(
        applications_route,
        "build_runtime_capabilities",
        lambda *_args, **_kwargs: {"submission": {"allowed": True, "reasons": []}},
    )
    monkeypatch.setattr(
        applications_route,
        "adapter_for_url",
        lambda _url: descriptor,
    )
    db = factory()
    qualification = db.query(AdapterQualificationRecord).one()
    qualification.invalidated_at = datetime.now(UTC).replace(tzinfo=None)
    qualification.invalidation_reason = "TEST_INVALIDATION"
    db.commit()
    db.close()
    adapter_response = client.post(url, json=_payload(reviewed), headers=headers)
    assert adapter_response.status_code == 409
    assert adapter_response.json()["detail"]["code"] == "ADAPTER_NOT_QUALIFIED"
    db = factory()
    try:
        assert db.query(ControlPlaneReviewGrant).count() == 0
    finally:
        db.close()
    _assert_no_external_action(factory)


@pytest.mark.parametrize(
    ("identity_field", "stale_value"),
    [
        ("platform", "greenhouse-next"),
        ("adapter_version", "1.0.1"),
        ("selector_version", "greenhouse-candidate-v10"),
    ],
)
def test_review_grant_rejects_stale_adapter_identity_before_mint(
    review_grant_api,
    monkeypatch,
    identity_field,
    stale_value,
):
    client, factory, reviewed, settings, descriptor = review_grant_api
    live_descriptor = replace(
        descriptor,
        qualification=QualificationTier.LIVE_CANARY_QUALIFIED,
        qualified_form_scope=(reviewed["fingerprint"],),
        execution_contract_version=TWO_PHASE_EXECUTION_CONTRACT_VERSION,
        **{identity_field: stale_value},
    )
    monkeypatch.setattr(
        applications_route,
        "adapter_for_url",
        lambda _url: live_descriptor,
    )

    response = client.post(
        f"/api/applications/{reviewed['application_id']}/control-plane-review-grant",
        json=_payload(reviewed),
        headers={"Authorization": f"Bearer {settings.secret_key}"},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "ADAPTER_VERSION_CHANGED"
    db = factory()
    try:
        assert db.query(ControlPlaneReviewGrant).count() == 0
    finally:
        db.close()
    _assert_no_external_action(factory)
