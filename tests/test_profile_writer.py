from pathlib import Path
import yaml
from profile.models import UserProfile
from profile.writer import save_profile
from profile import loader


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
