from __future__ import annotations

from profile.models import Personal, Preferences, Resume, UserProfile

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Base, UserProfileVersion
from discovery.public_sources import parse_remotive_jobs
from discovery.search_intents import derive_search_intents


def _profile() -> UserProfile:
    return UserProfile(
        # Discovery intentionally ignores placeholder identity.
        personal=Personal(name="Jane Doe", email="jane@example.com"),
        resume=Resume(text="Experienced engineer. " * 20),
        preferences=Preferences(
            roles=["Machine Learning Engineer", "Software Engineer"],
            locations=["Israel", "Worldwide Remote"],
            keywords=["Python", "Kubernetes", "PyTorch"],
        ),
    )


def test_parse_remotive_jobs_filters_profile_and_removes_html():
    payload = {
        "jobs": [
            {
                "title": "Machine Learning Engineer",
                "company_name": "Example AI",
                "candidate_required_location": "Worldwide",
                "job_type": "full_time",
                "description": "<p>Build <strong>PyTorch</strong> systems with Python.</p>",
                "url": "https://remotive.com/remote-jobs/software-dev/example",
                "publication_date": "2026-07-25T00:00:00",
                "tags": ["Python", "AI"],
            },
            {
                "title": "Account Executive",
                "company_name": "Sales Co",
                "description": "Own enterprise accounts.",
                "url": "https://remotive.com/remote-jobs/sales/example",
            },
        ]
    }

    jobs = parse_remotive_jobs(payload, _profile(), 10)

    assert len(jobs) == 1
    assert jobs[0].title == "Machine Learning Engineer"
    assert jobs[0].description == "Build PyTorch systems with Python."
    assert jobs[0].keywords == ["Python", "AI"]


def test_parse_remotive_jobs_honors_bound():
    row = {
        "title": "Software Engineer",
        "company_name": "Example",
        "description": "Python and Kubernetes",
        "url": "https://remotive.com/job/",
    }
    payload = {"jobs": [{**row, "url": f"https://remotive.com/job/{index}"} for index in range(5)]}

    assert len(parse_remotive_jobs(payload, _profile(), 2)) == 2


def test_remotive_filter_uses_every_cv_derived_intent():
    from profile.cv_routing import CVDefinition, CVRoutingConfig

    routing = CVRoutingConfig(
        cvs=[
            CVDefinition(
                id="embedded",
                file="embedded.pdf",
                title_terms=["Embedded Engineer"],
                skills=["C++", "RTOS"],
            )
        ]
    )
    payload = {
        "jobs": [
            {
                "title": "Embedded Engineer",
                "company_name": "Example",
                "description": "Build C++ RTOS firmware.",
                "url": "https://remotive.com/remote-jobs/software-dev/embedded",
            }
        ]
    }

    jobs = parse_remotive_jobs(
        payload,
        _profile(),
        10,
        intents=derive_search_intents(routing),
    )

    assert [job.title for job in jobs] == ["Embedded Engineer"]


def test_discovery_profile_prefers_immutable_version_over_edited_yaml(tmp_path):
    mutable = _profile()
    mutable.preferences.roles = ["Edited YAML Role"]
    profile_path = tmp_path / "user_profile.yaml"
    profile_path.write_text(
        yaml.safe_dump(mutable.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )

    immutable = _profile()
    immutable.preferences.roles = ["Immutable Role"]
    engine = create_engine(f"sqlite:///{tmp_path / 'profile-snapshot.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    db.add(
        UserProfileVersion(
            version=1,
            profile_yaml=yaml.safe_dump(
                immutable.model_dump(mode="json"),
                sort_keys=False,
            ),
        )
    )
    db.commit()

    from worker.discovery_tasks import _load_discovery_profile

    profile, version = _load_discovery_profile(
        Settings(
            _env_file=None,
            user_profile_path=str(profile_path),
        ),
        db,
    )

    assert version == 1
    assert profile.preferences.roles == ["Immutable Role"]
    db.close()
    engine.dispose()


def test_global_discovery_switch_still_drains_preparation_backlog(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")
    monkeypatch.setenv("AUTO_APPLY", "true")
    monkeypatch.setenv("TASKS_ALWAYS_EAGER", "false")
    monkeypatch.setenv("PREPARATION_REQUEUE_BATCH_SIZE", "7")

    import core.config as config_module
    import db.session as session_module
    from worker import discovery_tasks

    class Database:
        closed = False

        def close(self):
            self.closed = True

    database = Database()

    class AllowedGovernor:
        def can_act(self):
            return True, "ok"

    queued: list[dict[str, object]] = []

    def requeue(_db, **kwargs):
        queued.append(kwargs)
        return 7

    config_module.get_settings.cache_clear()
    monkeypatch.setattr(discovery_tasks, "get_governor", lambda: AllowedGovernor())
    monkeypatch.setattr(session_module, "get_session_factory", lambda: lambda: database)
    monkeypatch.setattr(
        discovery_tasks,
        "_load_discovery_profile",
        lambda _settings, _db: (_profile(), 1),
    )
    monkeypatch.setattr(
        "core.operations.readiness_report",
        lambda _settings, **_kwargs: {"status": "ready", "checks": {}},
    )
    monkeypatch.setattr(
        "core.automation_readiness.build_automation_readiness",
        lambda **_kwargs: {
            "preparation_ready": True,
            "stages": {
                "preparation": {
                    "ready": True,
                    "reason_codes": [],
                }
            },
        },
    )
    monkeypatch.setattr(
        "worker.rescore.requeue_scored_jobs_for_preparation",
        requeue,
    )
    try:
        assert discovery_tasks.discover_jobs_task() == 0
        assert queued == [{"tasks_always_eager": False, "batch_size": 7}]
        assert database.closed is True
    finally:
        config_module.get_settings.cache_clear()


def test_public_discovery_continues_during_linkedin_cooldown(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'public-discovery.db'}"
    routing_path = tmp_path / "cv_routing.yaml"
    routing_path.write_text(
        yaml.safe_dump(
            {
                "cvs": [
                    {
                        "id": "ml-engineer",
                        "file": "ml-engineer.pdf",
                        "title_terms": ["Machine Learning Engineer"],
                        "skills": ["Python", "PyTorch"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("CV_ROUTING_PATH", str(routing_path))
    monkeypatch.setenv("PUBLIC_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_DISCOVERY_INTERVAL_H", "6")
    monkeypatch.setenv("TASKS_ALWAYS_EAGER", "true")
    monkeypatch.setenv("AUTO_APPLY", "false")

    import profile.loader as profile_module

    import core.config as config_module
    import db.session as session_module
    import discovery.linkedin_search as linkedin_module
    import discovery.mesh as mesh_module
    from worker import discovery_tasks

    config_module.get_settings.cache_clear()
    session_module._engine = None
    session_module._SessionLocal = None
    session_module.init_db()

    calls = {"mesh": 0, "linkedin": 0}

    async def fake_linkedin(
        *_args,
        **_kwargs,
    ):
        calls["linkedin"] += 1
        raise AssertionError("scheduled LinkedIn crawling must remain disabled")

    async def fake_mesh(
        _db,
        *,
        settings,
        profile,
        preparation_ready,
        force,
        source_filter,
    ):
        assert settings.public_discovery_enabled is True
        assert profile.preferences.roles == _profile().preferences.roles
        assert preparation_ready is False
        assert force is False
        assert source_filter is None
        calls["mesh"] += 1
        return {
            "inserted": 2 if calls["mesh"] == 1 else 0,
            "updated": 0,
            "closed": 0,
            "skipped_overlap": 0,
        }

    class CooldownGovernor:
        def can_act(self):
            return False, "in challenge cooldown"

        def status(self):
            return {"in_cooldown": True}

    monkeypatch.setattr(linkedin_module, "run_discovery", fake_linkedin)
    monkeypatch.setattr(mesh_module, "run_discovery_mesh", fake_mesh)
    snapshot_calls = 0

    def latest_profile(_path):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return _profile()

    monkeypatch.setattr(profile_module, "load_profile_snapshot", latest_profile)
    monkeypatch.setattr(
        profile_module,
        "get_profile",
        lambda: (_ for _ in ()).throw(AssertionError("cached profile used")),
    )
    monkeypatch.setattr(
        "core.operations.readiness_report",
        lambda _settings: {"status": "degraded", "checks": {}},
    )
    monkeypatch.setattr(
        "core.automation_readiness.build_automation_readiness",
        lambda **_kwargs: {
            "preparation_ready": False,
            "stages": {
                "preparation": {
                    "ready": False,
                    "reason_codes": ["AUTO_PREPARE_DISABLED"],
                }
            },
        },
    )
    monkeypatch.setattr(discovery_tasks, "get_governor", lambda: CooldownGovernor())

    assert discovery_tasks.discover_jobs_task() == 2
    assert discovery_tasks.discover_jobs_task() == 0
    assert calls == {"mesh": 2, "linkedin": 0}
    assert snapshot_calls == 2
    assert mesh_module.LINKEDIN_DESCRIPTOR.enabled is False
    assert mesh_module.LINKEDIN_DESCRIPTOR.disabled_reason == "WRITTEN_PARTNER_ACCESS_REQUIRED"
