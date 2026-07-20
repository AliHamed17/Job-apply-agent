"""Rate governor — shared budget + circuit breaker + kill switch for LinkedIn."""

from __future__ import annotations

import random
from datetime import datetime

import structlog

logger = structlog.get_logger(__name__)


class _MemoryStore:
    """Minimal in-process fallback when Redis is unavailable (tests/dev)."""
    def __init__(self):
        self._d: dict[str, str] = {}
    def get(self, k):  # noqa: D401
        v = self._d.get(k)
        return v.encode() if isinstance(v, str) else v
    def set(self, k, v, ex=None):
        self._d[k] = str(v)
    def incr(self, k):
        self._d[k] = str(int(self._d.get(k, "0")) + 1)
        return int(self._d[k])
    def delete(self, k):
        self._d.pop(k, None)


class RateGovernor:
    def __init__(self, settings, redis_client=None, now_fn=None, sleep_fn=None, rng=None):
        self.s = settings
        self.store = redis_client if redis_client is not None else _MemoryStore()
        self._now = now_fn or datetime.utcnow
        self._sleep = sleep_fn
        self._rng = rng or random.Random()

    # ── day-scoped counter ────────────────────────────
    def _day_key(self) -> str:
        return f"li:apps:{self._now().strftime('%Y%m%d')}"

    def applications_today(self) -> int:
        raw = self.store.get(self._day_key())
        return int(raw) if raw else 0

    def budget_remaining(self) -> int:
        return max(0, self.s.linkedin_daily_cap - self.applications_today())

    def record_application(self) -> None:
        self.store.incr(self._day_key())

    # ── WhatsApp/email outbound day-scoped counter (Task 5.5) ─
    def _wa_key(self) -> str:
        return f"wa:out:{self._now().strftime('%Y%m%d')}"

    def wa_remaining(self) -> int:
        raw = self.store.get(self._wa_key())
        used = int(raw) if raw else 0
        return max(0, self.s.wa_outbound_daily_cap - used)

    def wa_record(self) -> None:
        self.store.incr(self._wa_key())

    # ── active hours ──────────────────────────────────
    def within_active_hours(self) -> bool:
        start, end = self.s.active_hours_range()
        return start <= self._now().hour < end

    # ── jittered gap ──────────────────────────────────
    def next_gap_seconds(self) -> int:
        return self._rng.randint(self.s.linkedin_min_gap_s, self.s.linkedin_max_gap_s)

    # ── combined gate (cooldown + kill wired in Task 2.2/2.4) ──
    def can_act(self) -> tuple[bool, str]:
        if self.is_killed():
            return False, "kill switch active"
        if self.in_cooldown():
            return False, "in challenge cooldown"
        if not self.within_active_hours():
            return False, "outside active hours"
        if self.budget_remaining() <= 0:
            return False, "daily cap reached"
        return True, "ok"

    # ── circuit breaker (cooldown) ────────────────────
    def _epoch(self) -> int:
        n = self._now()
        return int(n.timestamp()) if hasattr(n, "timestamp") else 0

    def trip_cooldown(self) -> int:
        """Record a challenge and set/extend cooldown. Returns hours applied."""
        window_key = "li:trips"
        trips = int(self.store.get(window_key) or 0)
        hours = min(48, 6 * (2 ** trips))
        self.store.set(window_key, trips + 1, ex=7 * 24 * 3600)  # 7-day window
        until = self._epoch() + hours * 3600
        self.store.set("li:cooldown_until", until)
        logger.warning("governor_cooldown_tripped", hours=hours, trips=trips + 1)
        return hours

    def in_cooldown(self) -> bool:
        until = self.store.get("li:cooldown_until")
        return bool(until) and self._epoch() < int(until)

    def cooldown_remaining_s(self) -> int:
        until = self.store.get("li:cooldown_until")
        return max(0, int(until) - self._epoch()) if until else 0

    # placeholders overridden in later tasks
    def is_killed(self) -> bool:
        return (self.store.get("li:kill") or b"") == b"1"

    # ── kill switch (Task 2.4) ────────────────────────
    def kill(self) -> None:
        self.store.set("li:kill", 1)

    def resume(self) -> None:
        self.store.delete("li:kill")

    def status(self) -> dict:
        return {
            "remaining": self.budget_remaining(),
            "applications_today": self.applications_today(),
            "killed": self.is_killed(),
            "in_cooldown": self.in_cooldown(),
            "cooldown_remaining_s": self.cooldown_remaining_s(),
            "within_active_hours": self.within_active_hours(),
        }


_governor: RateGovernor | None = None


def get_governor() -> RateGovernor:
    global _governor
    if _governor is None:
        from core.config import get_settings
        settings = get_settings()
        client = None
        try:
            import redis  # noqa: PLC0415
            client = redis.from_url(settings.redis_url)
            client.ping()
        except Exception as exc:
            client = None  # falls back to in-memory — degraded: per-process,
            # cross-process-unsafe (multiple workers won't share budget/
            # cooldown/kill-switch state). Log loudly so this is visible.
            logger.warning("governor_redis_unavailable_using_memory_store", error=str(exc))
        _governor = RateGovernor(settings, redis_client=client)
    return _governor
