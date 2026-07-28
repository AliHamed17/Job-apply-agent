"""Disclosure domain, persistence, API, and review UI coverage."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes.applications import _form_plan_response
from core.submission_domain import (
    DisclosureKind,
    DisclosureSource,
    FormDisclosureV1,
)
from db.models import Application, Base, FormPlan, Job, JobStatus

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "api" / "static" / "js" / "app.js").read_text(encoding="utf-8")


def _disclosure() -> FormDisclosureV1:
    summary = "Review the employer privacy policy."
    return FormDisclosureV1(
        disclosure_id="privacy",
        kind=DisclosureKind.PRIVACY_POLICY,
        source=DisclosureSource.LINK,
        position=0,
        summary=summary,
        content_sha256=hashlib.sha256(summary.encode()).hexdigest(),
        reference_sha256="a" * 64,
        acknowledgement_field_id="privacy_consent",
    )


def test_form_plan_response_round_trips_bounded_disclosures(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'disclosures.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    now = datetime.now(UTC).replace(tzinfo=None)
    job = Job(
        title="Fixture",
        company="Fixture",
        apply_url=("https://jobs.smartrecruiters.com/FixtureCo/123456789-sanitized-role"),
        source_url=("https://jobs.smartrecruiters.com/FixtureCo/123456789-sanitized-role"),
        status=JobStatus.DRAFT,
    )
    db.add(job)
    db.flush()
    application = Application(
        job_id=job.id,
        status=JobStatus.DRAFT,
        revision=1,
        prepared_revision=1,
        selected_cv_id="fixture-cv",
        selected_cv_hash="b" * 64,
        profile_version=1,
    )
    db.add(application)
    db.flush()
    disclosure = _disclosure()
    plan = FormPlan(
        plan_id="00000000-0000-4000-8000-000000000001",
        application_id=application.id,
        application_revision=1,
        adapter_name="smartrecruiters",
        adapter_version="1.0.0",
        selector_version="smartrecruiters-candidate-v1",
        fingerprint="c" * 64,
        selected_cv_id="fixture-cv",
        selected_cv_hash="b" * 64,
        attached_cv_id="fixture-cv",
        attached_cv_hash="b" * 64,
        attachment_verified=True,
        profile_version=1,
        fields_json="[]",
        disclosures_json=json.dumps([disclosure.model_dump(mode="json")]),
        decisions_json="[]",
        blockers_json="[]",
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    db.add(plan)
    db.commit()
    db.refresh(application)
    db.refresh(plan)

    response = _form_plan_response(plan, application)

    assert response.disclosures == [disclosure.model_dump(mode="json")]
    assert response.disclosures[0]["summary"] == disclosure.summary
    assert response.disclosures[0]["reference_sha256"] == "a" * 64
    serialized = response.model_dump_json()
    assert "https://" not in serialized
    assert "candidate.firstName" not in serialized
    db.close()
    engine.dispose()


def test_disclosure_contract_rejects_unbound_content_and_synthetic_claims() -> None:
    disclosure = _disclosure()
    payload = disclosure.model_dump(mode="json")

    with pytest.raises(ValueError, match="content digest"):
        FormDisclosureV1.model_validate(
            {
                **payload,
                "summary": "Changed after review.",
            }
        )
    with pytest.raises(ValueError, match="only the no-policy"):
        FormDisclosureV1.model_validate(
            {
                **payload,
                "source": DisclosureSource.SYNTHETIC,
            }
        )


def test_dashboard_renders_disclosures_read_only_and_escaped() -> None:
    panel = APP_JS.split(
        "function renderFormPlanPanel(appId, plan)",
        maxsplit=1,
    )[1].split(
        "function readFormAnswer",
        maxsplit=1,
    )[0]

    assert "Array.isArray(plan.disclosures) ? plan.disclosures : []" in panel
    assert "Employer disclosures" in panel
    assert "esc(disclosure.summary" in panel
    assert "String(disclosure.kind" in panel
    assert "redactedDigest(disclosure.content_sha256)" in panel
    assert "Bound to an operator-reviewed consent control" in panel
    assert "disclosure.reference_url" not in panel
    assert "disclosure.href" not in panel
