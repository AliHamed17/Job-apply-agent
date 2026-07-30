from __future__ import annotations

from profile.models import Personal, Preferences, Resume, UserProfile

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.config import Settings
from db.models import Base, UserProfileVersion
from discovery.public_sources import parse_remotive_jobs


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


def test_global_discovery_switch_stops_before_database_access(monkeypatch):
    monkeypatch.setenv("DISCOVERY_ENABLED", "false")

    import core.config as config_module
    import db.session as session_module
    from worker import discovery_tasks

    class AllowedGovernor:
        def can_act(self):
            return True, "ok"

    def unexpected_session_factory():
        raise AssertionError("disabled discovery must not open the database")

    config_module.get_settings.cache_clear()
    monkeypatch.setattr(discovery_tasks, "get_governor", lambda: AllowedGovernor())
    monkeypatch.setattr(session_module, "get_session_factory", unexpected_session_factory)
    try:
        assert discovery_tasks.discover_jobs_task() == 0
    finally:
        config_module.get_settings.cache_clear()


def test_public_discovery_continues_during_linkedin_cooldown(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'public-discovery.db'}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("PUBLIC_DISCOVERY_ENABLED", "true")
    monkeypatch.setenv("PUBLIC_DISCOVERY_INTERVAL_H", "6")
    monkeypatch.setenv("TASKS_ALWAYS_EAGER", "true")
    monkeypatch.setenv("AUTO_APPLY", "false")

    import profile.loader as profile_module

    import core.config as config_module
    import db.session as session_module
    import discovery.ingest as ingest_module
    import discovery.linkedin_search as linkedin_module
    import discovery.public_sources as public_module
    from db.models import DiscoveryRun
    from worker import discovery_tasks

    config_module.get_settings.cache_clear()
    session_module._engine = None
    session_module._SessionLocal = None
    session_module.init_db()

    calls = {"public": 0, "linkedin": 0}

    async def fake_public(_profile, _settings):
        calls["public"] += 1
        return []

    async def fake_linkedin(
        _db,
        _profile,
        _settings,
        _governor,
        *,
        preparation_ready,
    ):
        assert preparation_ready is False
        calls["linkedin"] += 1
        return 0

    def fake_ingest(_db, _jobs, **kwargs):
        assert kwargs["preparation_ready"] is False
        return 2 if kwargs["source"] == "remotive" else 0

    class CooldownGovernor:
        def can_act(self):
            return False, "in challenge cooldown"

        def status(self):
            return {"in_cooldown": True}

    monkeypatch.setattr(public_module, "fetch_remotive_jobs", fake_public)
    monkeypatch.setattr(linkedin_module, "run_discovery", fake_linkedin)
    monkeypatch.setattr(ingest_module, "ingest_discovered_jobs", fake_ingest)
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
        "core.automation_readiness.current_automation_readiness",
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
    monkeypatch.setattr(discovery_tasks, "get_governor", lambda: CooldownGovernor())

    assert discovery_tasks.discover_jobs_task() == 2
    assert discovery_tasks.discover_jobs_task() == 0
    assert calls == {"public": 1, "linkedin": 0}
    assert snapshot_calls == 2

    db = session_module.get_session_factory()()
    try:
        runs = db.query(DiscoveryRun).order_by(DiscoveryRun.id).all()
        assert [(run.source, run.status) for run in runs] == [
            ("remotive", "success"),
            ("linkedin_search", "skipped"),
            ("linkedin_search", "skipped"),
        ]
    finally:
        db.close()
