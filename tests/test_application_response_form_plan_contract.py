"""Application response contract for versioned ATS form-plan gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from core.form_plan_persistence import persist_inspected_form_plan
from core.submission_domain import FormPlanV1
from db.models import Application, Base, Job, JobStatus
from submitters.platforms import detect_platform

_PLATFORM_CASES = (
    pytest.param(
        "workday",
        "https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-1",
        "2.0.3",
        "workday-candidate-v2.4",
        id="workday",
    ),
    pytest.param(
        "greenhouse",
        "https://boards.greenhouse.io/fixture/jobs/1001",
        "1.0.0",
        "greenhouse-candidate-v9",
        id="greenhouse",
    ),
)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'application-response-contract.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _application(db, *, url: str) -> Application:
    application = Application(
        job=Job(
            title="Fixture role",
            company="Fixture company",
            source_url=url,
            apply_url=url,
            status=JobStatus.DRAFT,
        ),
        status=JobStatus.DRAFT,
        revision=1,
        selected_cv_id="fixture-cv",
        selected_cv_hash="a" * 64,
        profile_version=3,
        material_eligible=True,
        material_model_provider="ollama",
        material_model_name="qwen2.5:7b",
        material_model_digest=f"sha256:{'e' * 64}",
        material_prompt_version="material-package-v1",
    )
    db.add(application)
    db.commit()
    return application


def _plan(
    application: Application,
    *,
    platform: str,
    adapter_version: str,
    selector_version: str,
) -> FormPlanV1:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    return FormPlanV1(
        plan_id=uuid4(),
        application_id=application.id,
        application_revision=application.revision,
        adapter_name=platform,
        adapter_version=adapter_version,
        selector_version=selector_version,
        form_fingerprint="b" * 64,
        selected_cv_id=application.selected_cv_id,
        selected_cv_hash=application.selected_cv_hash,
        attached_cv_id=application.selected_cv_id,
        attached_cv_hash=application.selected_cv_hash,
        attachment_verified=True,
        profile_version=application.profile_version,
        session_verified_at=observed_at,
        created_at=observed_at,
        expires_at=observed_at + timedelta(minutes=30),
        fields=(),
        decisions=(),
    )


async def _detail_and_listed(db, application_id: int):
    detail = await applications_route.get_application(application_id, db)
    listing = await applications_route.list_applications(status=None, db=db)
    listed = next(item for item in listing if item.id == application_id)
    return detail, listed


@pytest.fixture(autouse=True)
def _avoid_local_portal_profile_probe(monkeypatch):
    monkeypatch.setattr(
        applications_route,
        "_portal_status",
        lambda application: (
            detect_platform(application.job.apply_url if application.job else ""),
            None,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "url", "adapter_version", "selector_version"),
    _PLATFORM_CASES,
)
async def test_list_and_get_fail_closed_without_versioned_form_plan(
    tmp_path,
    platform: str,
    url: str,
    adapter_version: str,
    selector_version: str,
) -> None:
    del adapter_version, selector_version
    db = _factory(tmp_path)()
    application = _application(db, url=url)

    responses = await _detail_and_listed(db, application.id)

    for response in responses:
        payload = response.model_dump()
        assert response.platform == platform
        assert payload["requires_versioned_form_plan"] is True
        assert payload["form_plan_review_ready"] is False
        assert payload["form_plan_adapter_name"] is None
        assert payload["form_plan_adapter_version"] is None
        assert payload["form_plan_selector_version"] is None
        assert payload["form_plan_valid"] is False
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "url", "adapter_version", "selector_version"),
    _PLATFORM_CASES,
)
async def test_list_and_get_expose_exact_review_ready_adapter_identity(
    tmp_path,
    platform: str,
    url: str,
    adapter_version: str,
    selector_version: str,
) -> None:
    db = _factory(tmp_path)()
    application = _application(db, url=url)
    persist_inspected_form_plan(
        db,
        application=application,
        plan=_plan(
            application,
            platform=platform,
            adapter_version=adapter_version,
            selector_version=selector_version,
        ),
    )
    db.commit()

    responses = await _detail_and_listed(db, application.id)

    for response in responses:
        payload = response.model_dump()
        assert response.platform == platform
        assert payload["requires_versioned_form_plan"] is True
        assert payload["form_plan_review_ready"] is True
        assert payload["form_plan_adapter_name"] == platform
        assert payload["form_plan_adapter_version"] == adapter_version
        assert payload["form_plan_selector_version"] == selector_version
        assert payload["form_plan_valid"] is False
    db.close()
