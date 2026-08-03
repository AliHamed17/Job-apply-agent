import random
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

import core.governor as governor_module
from core.config import Settings
from core.governor import GovernorUnavailableError, RateGovernor, get_governor


def _gov(**over):
    s = Settings(_env_file=None, **over)
    # In-memory store, deterministic clock + rng. The clock is a real
    # timezone-aware datetime in the operator's timezone, so clock["h"] reads as
    # the local wall-clock hour the active-hours window is compared against.
    # It used to be a duck-typed stub exposing only .hour and .strftime, which
    # is why _epoch() still carries a defensive hasattr check.
    clock = {"h": 12}
    tz = ZoneInfo(s.active_hours_timezone)
    return RateGovernor(
        s,
        redis_client=None,
        now_fn=lambda: datetime(2026, 7, 20, clock["h"], tzinfo=tz),
        rng=random.Random(1),
    ), clock


def _gov_at(utc_hour, utc_minute=0, **over):
    """A governor whose clock is an exact UTC instant, for timezone assertions."""
    s = Settings(_env_file=None, **over)
    return RateGovernor(
        s,
        redis_client=None,
        now_fn=lambda: datetime(2026, 7, 20, utc_hour, utc_minute, tzinfo=UTC),
        rng=random.Random(1),
    )


def test_cap_and_record():
    gov, _ = _gov(linkedin_daily_cap=2)
    assert gov.budget_remaining() == 2
    gov.record_application()
    assert gov.applications_today() == 1
    gov.record_application()
    ok, reason = gov.can_act()
    assert ok is False and "cap" in reason.lower()


def test_active_hours():
    gov, clock = _gov(active_hours="09:00-21:00")
    assert gov.within_active_hours() is True
    clock["h"] = 23
    assert gov.within_active_hours() is False


def test_active_hours_converts_utc_to_operator_timezone():
    """06:00 UTC is 09:00 in Jerusalem, inside 08:00-21:00.

    Comparing the UTC hour directly refused every send before 09:00 UTC
    (12:00 local), which blocked the operator's entire working morning while
    the signed policy considered those hours allowed.
    """
    gov = _gov_at(6, active_hours="08:00-21:00", active_hours_timezone="Asia/Jerusalem")
    assert gov.within_active_hours() is True


def test_active_hours_rejects_local_midnight():
    """21:30 UTC is 00:30 next day in Jerusalem, outside the window."""
    gov = _gov_at(21, 30, active_hours="08:00-21:00", active_hours_timezone="Asia/Jerusalem")
    assert gov.within_active_hours() is False


def test_active_hours_treats_naive_now_as_utc():
    s = Settings(_env_file=None, active_hours="08:00-21:00", active_hours_timezone="Asia/Jerusalem")
    gov = RateGovernor(
        s,
        redis_client=None,
        now_fn=lambda: datetime(2026, 7, 20, 6, 0),
        rng=random.Random(1),
    )
    assert gov.within_active_hours() is True


def test_active_hours_invalid_timezone_degrades_to_utc():
    """A mistyped timezone must not raise inside can_act()."""
    gov = _gov_at(6, active_hours="08:00-21:00", active_hours_timezone="Not/ARealZone")
    assert gov.within_active_hours() is False
    ok, reason = gov.can_act()
    assert ok is False and "active hours" in reason


def test_gap_within_bounds():
    gov, _ = _gov(linkedin_min_gap_s=120, linkedin_max_gap_s=360)
    for _ in range(20):
        g = gov.next_gap_seconds()
        assert 120 <= g <= 360


def test_wa_outbound_cap():
    gov, _ = _gov(wa_outbound_daily_cap=1)
    assert gov.wa_remaining() == 1
    gov.wa_record()
    assert gov.wa_remaining() == 0


def test_get_governor_logs_warning_when_redis_unavailable(monkeypatch):
    """IMPORTANT #6 — the in-memory fallback is degraded (per-process,
    cross-process-unsafe) and must be visible in logs, not silent."""
    import redis as redis_pkg

    class _BadClient:
        def ping(self):
            raise ConnectionError("no redis server")

    monkeypatch.setattr(redis_pkg, "from_url", lambda url: _BadClient())

    warnings = []
    monkeypatch.setattr(
        governor_module.logger,
        "warning",
        lambda event, **kw: warnings.append((event, kw)),
    )

    # Reset (and restore) the module-level singleton so get_governor()
    # actually re-runs its Redis-connect-and-fallback logic.
    monkeypatch.setattr(governor_module, "_governor", None)

    gov = get_governor()

    assert isinstance(gov, RateGovernor)
    assert any(event == "governor_redis_unavailable_using_memory_store" for event, _ in warnings)
    with pytest.raises(GovernorUnavailableError):
        get_governor(require_shared=True)
