"""Privacy and exact-binding tests for local control-plane review grants."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.control_plane_review_permits import (
    ControlPlaneReviewGrantError,
    claim_review_grant_projection,
    load_claimed_review_grant_projection,
    mark_review_grant_projected,
    mint_control_plane_review_grant,
    validate_control_plane_review_grant,
)
from db.models import Application, Base, ControlPlaneReviewGrant, FormPlan, Job, JobStatus
from submitters.platforms import adapter_for_url


def _reviewed_application(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'review-grant.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    now = datetime.now(UTC).replace(tzinfo=None)
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
        selected_cv_id="private-cv-name.pdf",
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
        application_revision=7,
        adapter_name=descriptor.platform,
        adapter_version=descriptor.adapter_version,
        selector_version=descriptor.selector_version,
        fingerprint="f" * 64,
        selected_cv_id=application.selected_cv_id,
        selected_cv_hash=application.selected_cv_hash,
        attached_cv_id=application.selected_cv_id,
        attached_cv_hash=application.selected_cv_hash,
        attachment_verified=True,
        profile_version=1,
        fields_json="[]",
        disclosures_json="[]",
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.commit()
    return factory, application.id, plan.id, now


def test_review_grant_projects_only_canonical_redacted_fields(tmp_path):
    factory, application_id, plan_id, now = _reviewed_application(tmp_path)
    db = factory()
    projection = mint_control_plane_review_grant(
        db,
        application_id=application_id,
        form_plan_id=plan_id,
        runner_release="a" * 64,
        now=now,
    )
    db.commit()

    assert UUID(projection.remote_application_ref).version == 4
    assert UUID(projection.review_grant_ref).version == 4
    assert projection.expires_at == now + timedelta(minutes=5)
    wire = projection.to_wire()
    assert set(wire) == {
        "application_ref",
        "grant_id",
        "application_revision",
        "adapter",
        "adapter_version",
        "form_fingerprint_digest",
        "reviewed_at",
    }
    serialized = repr(projection) + str(wire)
    assert "Private Candidate" not in serialized
    assert "Private Employer" not in serialized
    assert "private-cv-name.pdf" not in serialized
    assert "boards.greenhouse.io" not in serialized
    assert "c" * 64 not in serialized

    grant = validate_control_plane_review_grant(
        db,
        review_grant_ref=projection.review_grant_ref,
        remote_application_ref=projection.remote_application_ref,
        runner_release="a" * 64,
        now=now + timedelta(seconds=1),
    )
    assert grant.application_id == application_id
    db.close()


def test_review_grant_revalidates_private_bindings_and_expires(tmp_path):
    factory, application_id, plan_id, now = _reviewed_application(tmp_path)
    db = factory()
    projection = mint_control_plane_review_grant(
        db,
        application_id=application_id,
        form_plan_id=plan_id,
        runner_release="a" * 64,
        now=now,
    )
    db.commit()
    application = db.get(Application, application_id)
    application.job.apply_url = "https://boards.greenhouse.io/acme/jobs/999"
    db.commit()

    with pytest.raises(ControlPlaneReviewGrantError, match="JOB_URL_CHANGED"):
        validate_control_plane_review_grant(
            db,
            review_grant_ref=projection.review_grant_ref,
            remote_application_ref=projection.remote_application_ref,
            runner_release="a" * 64,
            now=now + timedelta(seconds=1),
        )
    application.job.apply_url = "https://boards.greenhouse.io/acme/jobs/123"
    db.commit()
    with pytest.raises(ControlPlaneReviewGrantError, match="REVIEW_GRANT_EXPIRED"):
        validate_control_plane_review_grant(
            db,
            review_grant_ref=projection.review_grant_ref,
            remote_application_ref=projection.remote_application_ref,
            runner_release="a" * 64,
            now=now + timedelta(minutes=5),
        )
    db.close()


def test_new_review_grant_revokes_prior_unconsumed_authority(tmp_path):
    factory, application_id, plan_id, now = _reviewed_application(tmp_path)
    db = factory()
    first = mint_control_plane_review_grant(
        db,
        application_id=application_id,
        form_plan_id=plan_id,
        runner_release="a" * 64,
        now=now,
    )
    second = mint_control_plane_review_grant(
        db,
        application_id=application_id,
        form_plan_id=plan_id,
        runner_release="a" * 64,
        now=now + timedelta(seconds=1),
    )
    db.commit()

    assert first.review_grant_ref != second.review_grant_ref
    with pytest.raises(ControlPlaneReviewGrantError, match="REVIEW_GRANT_REVOKED"):
        validate_control_plane_review_grant(
            db,
            review_grant_ref=first.review_grant_ref,
            remote_application_ref=first.remote_application_ref,
            runner_release="a" * 64,
            now=now + timedelta(seconds=2),
        )
    db.close()


def test_review_grant_projection_is_durable_and_claimed_once(tmp_path):
    factory, application_id, plan_id, now = _reviewed_application(tmp_path)
    db = factory()
    projection = mint_control_plane_review_grant(
        db,
        application_id=application_id,
        form_plan_id=plan_id,
        runner_release="a" * 64,
        now=now,
    )
    db.commit()
    claim = claim_review_grant_projection(
        db,
        runner_id=str(uuid4()),
        now=now,
    )
    assert claim is not None
    assert (
        claim_review_grant_projection(
            db,
            runner_id=str(uuid4()),
            now=now,
        )
        is None
    )
    claimed = load_claimed_review_grant_projection(
        db,
        grant_id=claim[0],
        claim_token=claim[1],
        runner_release="a" * 64,
        now=now,
    )
    assert claimed.review_grant_ref == projection.review_grant_ref
    mark_review_grant_projected(
        db,
        grant_id=claim[0],
        claim_token=claim[1],
        now=now,
    )
    row = db.get(ControlPlaneReviewGrant, claim[0])
    assert row.projection_state == "projected"
    assert row.projected_at == now
    db.close()
