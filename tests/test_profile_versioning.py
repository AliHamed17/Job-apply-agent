"""Exact, immutable UserProfileVersion identity tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from profile import loader
from profile import writer as writer_module
from profile.models import UserProfile
from profile.versioned_snapshot import (
    ProfileSnapshotError,
    latest_profile_version,
    load_versioned_profile_snapshot,
)
from profile.writer import profile_write_transaction, save_profile

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from db.models import Base, UserProfileVersion


def _factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'profiles.db').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


def _profile(name: str) -> UserProfile:
    profile = UserProfile()
    profile.personal.name = name
    return profile


def test_profile_version_is_unique_and_exact_snapshot_ignores_cache(
    tmp_path,
    monkeypatch,
):
    engine, factory = _factory(tmp_path)
    db = factory()
    db.add_all(
        [
            UserProfileVersion(
                version=1,
                profile_yaml=yaml.safe_dump(_profile("Version one").model_dump()),
            ),
            UserProfileVersion(
                version=2,
                profile_yaml=yaml.safe_dump(_profile("Version two").model_dump()),
            ),
        ]
    )
    db.commit()

    monkeypatch.setattr(loader, "_profile", _profile("Stale process cache"))
    latest = load_versioned_profile_snapshot(db)
    exact = load_versioned_profile_snapshot(db, version=1)

    assert latest_profile_version(db) == 2
    assert latest.version == 2
    assert latest.profile.personal.name == "Version two"
    assert exact.version == 1
    assert exact.profile.personal.name == "Version one"
    with pytest.raises(ProfileSnapshotError, match="PROFILE_VERSION_NOT_FOUND"):
        load_versioned_profile_snapshot(db, version=99)

    db.add(
        UserProfileVersion(
            version=2,
            profile_yaml=yaml.safe_dump(_profile("Duplicate").model_dump()),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()
    engine.dispose()


def test_concurrent_profile_writes_allocate_distinct_versions(tmp_path):
    engine, factory = _factory(tmp_path)
    yaml_path = tmp_path / "user_profile.yaml"

    def write(name: str) -> int:
        db = factory()
        try:
            return save_profile(_profile(name), yaml_path, db=db)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = list(pool.map(write, ("First", "Second")))

    db = factory()
    rows = db.query(UserProfileVersion).order_by(UserProfileVersion.version).all()
    assert sorted(versions) == [1, 2]
    assert [row.version for row in rows] == [1, 2]
    assert len({row.version for row in rows}) == 2
    latest = load_versioned_profile_snapshot(db)
    persisted_file = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert persisted_file["personal"]["name"] == latest.profile.personal.name
    assert latest.version == 2
    db.close()
    engine.dispose()


def test_profile_writes_take_automation_fence_before_profile_lock(
    tmp_path,
    monkeypatch,
):
    engine, factory = _factory(tmp_path)
    db = factory()
    yaml_path = tmp_path / "fenced-profile.yaml"
    calls: list[str] = []

    monkeypatch.setattr(
        writer_module,
        "lock_automation_authority_fence",
        lambda _db: calls.append("authority"),
    )
    monkeypatch.setattr(
        writer_module,
        "_acquire_database_write_lock",
        lambda _db: calls.append("profile"),
    )

    with profile_write_transaction(db):
        pass
    assert calls == ["authority", "profile"]

    calls.clear()
    save_profile(_profile("Direct writer"), yaml_path, db=db)
    assert calls[:2] == ["authority", "profile"]

    db.close()
    engine.dispose()


def test_profile_write_retries_uniqueness_race_with_matching_file(
    tmp_path,
    monkeypatch,
):
    engine, factory = _factory(tmp_path)
    db = factory()
    yaml_path = tmp_path / "retry-profile.yaml"
    real_commit = db.commit
    commit_calls = 0

    def flaky_commit():
        nonlocal commit_calls
        commit_calls += 1
        if commit_calls == 1:
            raise IntegrityError("insert", {}, RuntimeError("unique race"))
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)
    version = save_profile(_profile("Retry winner"), yaml_path, db=db)

    assert commit_calls == 2
    assert version == 1
    row = db.query(UserProfileVersion).one()
    assert row.version == version
    assert yaml.safe_load(row.profile_yaml)["personal"]["name"] == "Retry winner"
    assert (
        yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["personal"]["name"] == "Retry winner"
    )
    db.close()
    engine.dispose()


def test_exhausted_profile_race_restores_committed_snapshot(
    tmp_path,
    monkeypatch,
):
    engine, factory = _factory(tmp_path)
    db = factory()
    yaml_path = tmp_path / "failed-profile.yaml"
    committed = _profile("Committed")
    committed_yaml = yaml.safe_dump(committed.model_dump(), sort_keys=False)
    yaml_path.write_text(committed_yaml, encoding="utf-8")
    db.add(UserProfileVersion(version=1, profile_yaml=committed_yaml))
    db.commit()

    def always_conflict():
        raise IntegrityError("insert", {}, RuntimeError("unique race"))

    monkeypatch.setattr(db, "commit", always_conflict)
    with pytest.raises(IntegrityError):
        save_profile(_profile("Must not escape"), yaml_path, db=db)

    assert db.query(UserProfileVersion).count() == 1
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8"))["personal"]["name"] == "Committed"
    db.close()
    engine.dispose()
