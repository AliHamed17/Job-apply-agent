"""Integration coverage for the worker's low-confidence CV routing fallback."""

from __future__ import annotations

import hashlib
from profile.cv_routing import RoutingDecision
from profile.models import UserProfile
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Application, Base, Job, JobStatus, UserProfileVersion
from llm.generation import GeneratedApplication


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gen.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def _routing_config_path(tmp_path, minimum_confidence=0.9):
    config = {
        "cvs": [
            {"id": "cv_a", "file": "a.pdf", "title_terms": ["backend"], "skills": ["python"]},
            {"id": "cv_b", "file": "b.pdf", "title_terms": ["ai"], "skills": ["pytorch"]},
        ],
        "fallback_cv_id": "cv_a",
        "minimum_confidence": minimum_confidence,
    }
    path = tmp_path / "cv_routing.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def _make_job(db):
    job = Job(
        title="AI Engineer",
        company="Gitlab",
        source_url="https://x",
        status=JobStatus.SCORED,
        score=57.5,
        location="",
        employment_type="",
        seniority="",
        description="",
        requirements="",
        apply_url="",
    )
    db.add(job)
    db.flush()
    db.commit()
    return job.id


def _settings(tmp_path, minimum_confidence=0.9):
    return Settings(
        _env_file=None,
        draft_only=True,
        auto_apply=False,
        cv_routing_path=str(_routing_config_path(tmp_path, minimum_confidence)),
        cv_directory=str(tmp_path),
    )


def _write_cvs(tmp_path):
    a_bytes = b"%PDF-1.4 synthetic backend CV"
    b_bytes = b"%PDF-1.4 synthetic AI CV"
    (tmp_path / "a.pdf").write_bytes(a_bytes)
    (tmp_path / "b.pdf").write_bytes(b_bytes)
    return {
        "cv_a": hashlib.sha256(a_bytes).hexdigest(),
        "cv_b": hashlib.sha256(b_bytes).hexdigest(),
    }


@pytest.mark.asyncio
async def test_llm_routing_used_when_deterministic_falls_back(tmp_path):
    cv_hashes = _write_cvs(tmp_path)
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.close()

    generated = GeneratedApplication(
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers={},
        has_placeholders=False,
        placeholder_fields=[],
    )
    llm_decision = RoutingDecision(
        selected_cv_id="cv_b",
        selected_file="b.pdf",
        selected_cv_hash=cv_hashes["cv_b"],
        confidence=0.9,
        matched_evidence=["llm:matched AI/ML background"],
    )

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=_settings(tmp_path)),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "profile.cv_content_cache.extract_text_from_pdf",
            side_effect=lambda path: (
                "Primary programming language: Python"
                if path.name == "b.pdf"
                else "Backend framework: FastAPI"
            ),
        ),
        patch("llm.generation.generate_full_application", new=AsyncMock(return_value=generated)),
        patch(
            "profile.cv_routing_llm.select_cv_via_llm",
            new=AsyncMock(return_value=llm_decision),
        ) as mock_llm,
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id])

    mock_llm.assert_called_once()
    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).first()
    assert app.selected_cv_id == "cv_b"
    assert app.selected_cv_hash == cv_hashes["cv_b"]
    assert app.cv_routing_confidence == 0.9
    db.close()


@pytest.mark.asyncio
async def test_llm_routing_skipped_when_deterministic_is_confident(tmp_path):
    _write_cvs(tmp_path)
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.close()

    generated = GeneratedApplication(
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers={},
        has_placeholders=False,
        placeholder_fields=[],
    )

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=_settings(tmp_path, 0.1)),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "profile.cv_content_cache.extract_text_from_pdf",
            return_value="Primary programming language: Python",
        ),
        patch("llm.generation.generate_full_application", new=AsyncMock(return_value=generated)),
        patch("profile.cv_routing_llm.select_cv_via_llm", new=AsyncMock()) as mock_llm,
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id])

    mock_llm.assert_not_called()
    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).first()
    assert app.selected_cv_id == "cv_b"
    db.close()


@pytest.mark.asyncio
async def test_low_confidence_fallback_stays_review_required(tmp_path):
    _write_cvs(tmp_path)
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.close()

    generated = GeneratedApplication(
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers={},
        has_placeholders=False,
        placeholder_fields=[],
    )
    settings = Settings(
        _env_file=None,
        draft_only=False,
        auto_apply=True,
        auto_apply_threshold=50.0,
        tasks_always_eager=False,
        llm_cv_routing=False,
        llm_cv_alignment=False,
        cv_routing_path=str(_routing_config_path(tmp_path)),
        cv_directory=str(tmp_path),
    )

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch("worker.tasks.get_settings", return_value=settings),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "profile.cv_content_cache.extract_text_from_pdf",
            return_value="Primary programming language: Python",
        ),
        patch("llm.generation.generate_full_application", new=AsyncMock(return_value=generated)),
    ):
        from worker.tasks import generate_application_task

        generate_application_task.apply(args=[job_id])

    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).first()
    assert app.status == JobStatus.DRAFT
    assert app.cv_routing_fallback_reason == "confidence_below_threshold"
    assert app.needs_review_reason == "MATERIAL_NOT_ELIGIBLE"
    db.close()


def test_worker_refuses_cv_replacement_during_generation(tmp_path):
    cv_hashes = _write_cvs(tmp_path)
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.close()
    generated = GeneratedApplication(
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers={},
        has_placeholders=False,
        placeholder_fields=[],
        cv_sha256=cv_hashes["cv_b"],
    )

    async def mutate_selected_cv(*_args, **_kwargs):
        (tmp_path / "b.pdf").write_bytes(b"%PDF-1.4 replaced while model ran")
        return generated

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch(
            "worker.tasks.get_settings",
            return_value=_settings(tmp_path, 0.1),
        ),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "profile.cv_content_cache.extract_text_from_pdf",
            return_value="Primary programming language: Python",
        ),
        patch(
            "llm.generation.generate_full_application",
            new=AsyncMock(side_effect=mutate_selected_cv),
        ),
    ):
        from worker.tasks import generate_application_task

        result = generate_application_task.apply(args=[job_id], throw=False)

    assert result.successful()
    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).one()
    assert app.selected_cv_hash != cv_hashes["cv_b"]
    assert app.material_eligible is False
    assert app.needs_review_reason == "MATERIAL_CV_MISMATCH"
    db.close()


@pytest.mark.asyncio
async def test_worker_selected_artifact_flows_into_inspector_form_policy(tmp_path):
    cv_hashes = _write_cvs(tmp_path)
    factory = _db(tmp_path)
    db = factory()
    job_id = _make_job(db)
    db.add(UserProfileVersion(version=1, profile_yaml="{}"))
    db.commit()
    db.close()
    generated = GeneratedApplication(
        cover_letter="letter",
        recruiter_message="msg",
        qa_answers={},
        has_placeholders=False,
        placeholder_fields=[],
        cv_sha256=cv_hashes["cv_b"],
    )
    settings = _settings(tmp_path, 0.1)

    with (
        patch("worker.tasks.get_session_factory", return_value=factory),
        patch(
            "worker.tasks.get_settings",
            return_value=settings,
        ),
        patch("profile.loader.get_profile", return_value=UserProfile()),
        patch(
            "profile.cv_content_cache.extract_text_from_pdf",
            side_effect=lambda path: (
                "Primary programming language: Python"
                if path.name == "b.pdf"
                else "Backend framework: FastAPI"
            ),
        ),
        patch(
            "llm.generation.generate_full_application",
            new=AsyncMock(return_value=generated),
        ),
    ):
        from worker.tasks import generate_application_task

        result = generate_application_task.apply(args=[job_id], throw=True)
        assert result.successful()

    db = factory()
    app = db.query(Application).filter(Application.job_id == job_id).one()
    assert app.selected_cv_id == "cv_b"
    assert app.selected_cv_hash == cv_hashes["cv_b"]

    from core.form_plan_evidence import resolve_selected_cv_planning_binding
    from core.form_planning import AnswerPolicyV1
    from core.submission_domain import AnswerProvenance, FormFieldV1

    binding = resolve_selected_cv_planning_binding(
        selected_cv_id=app.selected_cv_id,
        expected_cv_hash=app.selected_cv_hash,
        cv_routing_path=settings.cv_routing_path,
        cv_directory=settings.cv_directory,
    )
    context = binding.answer_policy_context(
        profile=UserProfile(),
        profile_version=app.profile_version,
        adapter_name="fixture-inspector",
        adapter_version="1.0.0",
        selector_version="fixture-v1",
        form_fingerprint="f" * 64,
        attached_cv_id=app.selected_cv_id,
        attached_cv_hash=app.selected_cv_hash,
        attachment_verified=True,
    )
    field = FormFieldV1.model_validate(
        {
            "field_id": "primary-language",
            "canonical_name": "primary_language",
            "label": "Primary programming language",
            "field_type": "text",
            "required": True,
            "position": 0,
        }
    )
    policy = await AnswerPolicyV1().plan_fields((field,), context)

    assert policy.decisions[0].value == "Python"
    assert policy.decisions[0].provenance == AnswerProvenance.CV_EVIDENCE
    assert policy.decisions[0].evidence_refs == (f"cv:{cv_hashes['cv_b']}:primary_language",)
    assert binding.redacted_diagnostics()["fact_catalog_digest"] == (
        context.selected_cv_fact_catalog_digest
    )
    db.close()
