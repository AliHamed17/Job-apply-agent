from profile import loader
from profile import writer as writer_module
from profile.models import UserProfile
from profile.writer import save_profile

import pytest
import yaml


def test_save_profile_writes_yaml_and_swaps_cache(tmp_path, monkeypatch):
    # Isolate the DB write — save_profile must tolerate db=None (no version row)
    p = UserProfile()
    p.personal.name = "Ali Hamed"
    yaml_path = tmp_path / "user_profile.yaml"

    version = save_profile(p, yaml_path, db=None)
    assert version == 1
    assert yaml_path.exists()
    loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert loaded["personal"]["name"] == "Ali Hamed"
    # Cache swapped
    assert loader.get_profile().personal.name == "Ali Hamed"


def test_save_profile_backs_up_existing(tmp_path):
    yaml_path = tmp_path / "user_profile.yaml"
    yaml_path.write_text("personal:\n  name: Old\n", encoding="utf-8")
    save_profile(UserProfile(), yaml_path, db=None)
    backups = list(tmp_path.glob("user_profile.yaml.bak-*"))
    assert len(backups) == 1
    assert "Old" in backups[0].read_text(encoding="utf-8")


def test_atomic_profile_write_retries_transient_permission_error(tmp_path, monkeypatch):
    yaml_path = tmp_path / "user_profile.yaml"
    real_replace = writer_module.os.replace
    attempts = 0

    def transient_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("transient scanner contention")
        real_replace(source, destination)

    monkeypatch.setattr(writer_module.os, "replace", transient_replace)
    monkeypatch.setattr(writer_module.time, "sleep", lambda _seconds: None)

    save_profile(UserProfile(), yaml_path, db=None)

    assert attempts == 3
    assert yaml_path.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_profile_write_preserves_persistent_permission_error(tmp_path, monkeypatch):
    yaml_path = tmp_path / "user_profile.yaml"
    attempts = 0

    def denied_replace(_source, _destination):
        nonlocal attempts
        attempts += 1
        raise PermissionError("persistent scanner contention")

    monkeypatch.setattr(writer_module.os, "replace", denied_replace)
    monkeypatch.setattr(writer_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="persistent scanner contention"):
        save_profile(UserProfile(), yaml_path, db=None)

    assert attempts == writer_module._ATOMIC_REPLACE_RETRIES
    assert not list(tmp_path.glob(".*.tmp"))
