import random

import core.governor as governor_module
from core.config import Settings
from core.governor import RateGovernor, get_governor


def _gov(**over):
    s = Settings(_env_file=None, **over)
    # in-memory store, deterministic clock + rng
    clock = {"h": 12}
    return RateGovernor(
        s, redis_client=None,
        now_fn=lambda: type("T", (), {"hour": clock["h"], "strftime": lambda self, f: "20260720"})(),
        rng=random.Random(1),
    ), clock


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
        governor_module.logger, "warning",
        lambda event, **kw: warnings.append((event, kw)),
    )

    # Reset (and restore) the module-level singleton so get_governor()
    # actually re-runs its Redis-connect-and-fallback logic.
    monkeypatch.setattr(governor_module, "_governor", None)

    gov = get_governor()

    assert isinstance(gov, RateGovernor)
    assert any(event == "governor_redis_unavailable_using_memory_store" for event, _ in warnings)
