from pathlib import Path

import pytest

from core.config import JOB_AGENT_ENV_FILE, Settings, get_settings


def test_new_defaults_present(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    s = Settings(_env_file=None)
    assert s.min_apply_score == 40.0
    assert s.linkedin_daily_cap == 45
    assert s.linkedin_min_gap_s == 120
    assert s.linkedin_max_gap_s == 360
    assert s.discovery_interval_h == 3
    assert s.wa_outbound_daily_cap == 15
    assert s.wa_contact_dedup_days == 30
    assert s.dry_run is False
    assert s.redis_url == "redis://127.0.0.1:6379/0"


def test_active_hours_range_parses():
    s = Settings(_env_file=None, active_hours="09:00-21:00")
    assert s.active_hours_range() == (9, 21)


def test_get_settings_accepts_only_an_absolute_external_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runtime_env = (tmp_path / "runtime.env").resolve()
    runtime_env.write_text("MIN_APPLY_SCORE=73\nDRY_RUN=true\n", encoding="utf-8")
    monkeypatch.setenv(JOB_AGENT_ENV_FILE, str(runtime_env))
    monkeypatch.setenv("MIN_APPLY_SCORE", "99")
    monkeypatch.setenv("DRY_RUN", "false")
    get_settings.cache_clear()
    try:
        loaded = get_settings()
        assert loaded.min_apply_score == 73
        assert loaded.dry_run is True
    finally:
        get_settings.cache_clear()

    monkeypatch.setenv(JOB_AGENT_ENV_FILE, "relative-runtime.env")
    with pytest.raises(ValueError, match="must be an absolute path"):
        get_settings()
    get_settings.cache_clear()
