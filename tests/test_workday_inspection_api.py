from datetime import UTC, datetime, timedelta
from profile.models import UserProfile
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import applications as applications_route
from core.submission_domain import FormPlanV1
from db.models import (
    Application,
    Base,
    FormPlan,
    Job,
    JobStatus,
    Submission,
    SubmissionCommand,
)


def _factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'inspection-api.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _application(factory) -> Application:
    db = factory()
    job = Job(
        title="Fixture role",
        company="Fixture company",
        source_url="https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-2",
        apply_url="https://fixture.wd5.myworkdayjobs.com/en-US/jobs/job/REQ-2",
        status=JobStatus.DRAFT,
    )
    app = Application(
        job=job,
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
    db.add(app)
    db.commit()
    db.close()
    return app


def _domain_plan(app_id: int, revision: int) -> FormPlanV1:
    now = datetime.now(UTC)
    return FormPlanV1(
        plan_id=uuid4(),
        application_id=app_id,
        application_revision=revision,
        adapter_name="workday",
        adapter_version="2.0.3",
        selector_version="workday-candidate-v2.4",
        form_fingerprint="b" * 64,
        selected_cv_id="fixture-cv",
        selected_cv_hash="a" * 64,
        attached_cv_id="fixture-cv",
        attached_cv_hash="a" * 64,
        attachment_verified=True,
        profile_version=3,
        session_verified_at=now,
        created_at=now,
        expires_at=now + timedelta(minutes=30),
        fields=(),
        decisions=(),
    )


class _Inspector:
    def __init__(self, plan: FormPlanV1, before_return=None):
        self.plan = plan
        self.before_return = before_return
        self.calls = []

    async def inspect(self, **kwargs):
        self.calls.append(kwargs)
        if self.before_return is not None:
            self.before_return()
        return self.plan


class _PolicyAwareInspector(_Inspector):
    async def inspect(self, *, answer_policy=None, **kwargs):
        self.calls.append(
            {
                **kwargs,
                "answer_policy": answer_policy,
                "db_transaction_open": answer_policy.db.in_transaction(),
            }
        )
        if self.before_return is not None:
            self.before_return()
        return self.plan


def _patch_private_inputs(monkeypatch, inspector, tmp_path) -> None:
    cv_path = tmp_path / "fixture.pdf"
    cv_path.write_bytes(b"sanitized fixture bytes")
    selected = SimpleNamespace(resolved_path=str(cv_path))
    monkeypatch.setattr(applications_route, "_validate_selected_cv", lambda _app: None)
    monkeypatch.setattr(applications_route, "_validate_material_quality", lambda _app: None)
    monkeypatch.setattr(
        applications_route,
        "get_selected_cv_artifact_by_id",
        lambda _cv_id: selected,
    )
    monkeypatch.setattr(
        applications_route,
        "require_current_selected_cv_artifact",
        lambda artifact, **_kwargs: artifact,
    )
    monkeypatch.setattr(
        applications_route,
        "load_versioned_profile_snapshot",
        lambda _db, **_kwargs: SimpleNamespace(profile=UserProfile()),
    )
    monkeypatch.setattr(
        applications_route,
        "_scoped_inspection_registry",
        lambda *_args, **_kwargs: SimpleNamespace(get_inspector=lambda _job: inspector),
    )


@pytest.mark.asyncio
async def test_inspection_persists_plan_without_attempt_or_command(tmp_path, monkeypatch) -> None:
    factory = _factory(tmp_path)
    app = _application(factory)
    inspector = _Inspector(_domain_plan(app.id, app.revision))
    _patch_private_inputs(monkeypatch, inspector, tmp_path)
    db = factory()

    response = await applications_route.inspect_application_form(
        app.id,
        applications_route.InspectApplicationRequest(application_revision=1),
        db,
    )

    assert response.adapter_name == "workday"
    assert response.attachment_verified is True
    assert response.attachment_verification_source == "candidate_browser_upload_complete"
    assert response.attachment_verified_at is not None
    assert response.selected_cv_ref == response.attached_cv_ref
    assert response.selected_cv_ref != response.selected_cv_id
    assert response.valid is False
    assert inspector.calls[0]["selected_cv_id"] == "fixture-cv"
    assert db.query(FormPlan).count() == 1
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()


@pytest.mark.asyncio
async def test_inspection_injects_request_scoped_db_policy_without_llm_or_lock(
    tmp_path,
    monkeypatch,
) -> None:
    factory = _factory(tmp_path)
    app = _application(factory)
    inspector = _PolicyAwareInspector(_domain_plan(app.id, app.revision))
    _patch_private_inputs(monkeypatch, inspector, tmp_path)
    db = factory()

    await applications_route.inspect_application_form(
        app.id,
        applications_route.InspectApplicationRequest(application_revision=1),
        db,
    )

    policy = inspector.calls[0]["answer_policy"]
    assert policy is not None
    assert policy.db is db
    assert policy.llm_client is None
    assert inspector.calls[0]["db_transaction_open"] is False
    db.close()


@pytest.mark.asyncio
async def test_preparation_completed_during_inspection_is_atomically_revoked(
    tmp_path,
    monkeypatch,
) -> None:
    factory = _factory(tmp_path)
    app = _application(factory)

    def prepare_while_browser_is_open() -> None:
        other = factory()
        current = other.get(Application, app.id)
        current.prepared_revision = current.revision
        current.approved_at = datetime.now(UTC).replace(tzinfo=None)
        current.approval_source = "manual_prepare"
        other.commit()
        other.close()

    inspector = _Inspector(
        _domain_plan(app.id, app.revision),
        before_return=prepare_while_browser_is_open,
    )
    _patch_private_inputs(monkeypatch, inspector, tmp_path)
    db = factory()

    response = await applications_route.inspect_application_form(
        app.id,
        applications_route.InspectApplicationRequest(application_revision=1),
        db,
    )

    db.expire_all()
    current = db.get(Application, app.id)
    assert response.valid is False
    assert current.prepared_revision is None
    assert current.approved_at is None
    assert current.approval_source is None
    assert db.query(FormPlan).count() == 1
    db.close()


@pytest.mark.asyncio
async def test_revision_change_during_browser_inspection_discards_plan(
    tmp_path,
    monkeypatch,
) -> None:
    factory = _factory(tmp_path)
    app = _application(factory)

    def mutate_revision() -> None:
        other = factory()
        current = other.get(Application, app.id)
        current.revision = 2
        other.commit()
        other.close()

    inspector = _Inspector(
        _domain_plan(app.id, app.revision),
        before_return=mutate_revision,
    )
    _patch_private_inputs(monkeypatch, inspector, tmp_path)
    db = factory()

    with pytest.raises(HTTPException) as exc_info:
        await applications_route.inspect_application_form(
            app.id,
            applications_route.InspectApplicationRequest(application_revision=1),
            db,
        )

    assert exc_info.value.status_code == 409
    assert db.query(FormPlan).count() == 0
    assert db.query(Submission).count() == 0
    assert db.query(SubmissionCommand).count() == 0
    db.close()
