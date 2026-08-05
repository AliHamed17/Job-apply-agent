"""Rate governor — shared budget + circuit breaker + kill switch for LinkedIn."""

from __future__ import annotations

import random
import threading
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import structlog

logger = structlog.get_logger(__name__)


class GovernorUnavailableError(RuntimeError):
    """A shared safety gate is required but Redis is unavailable."""


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
        self.shared_backend = redis_client is not None
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._sleep = sleep_fn
        self._rng = rng or random.Random()
        self._reservation_lock = threading.Lock()

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
        # Stamp the earliest epoch the next LinkedIn action may start, so the
        # configured random gap is actually enforced between real Easy Apply
        # submissions — not just relied on the 5-min beat cadence (which is
        # shorter than the max gap) or bypassed by directly-enqueued submits.
        self.store.set("li:next_action_at", self._epoch() + self.next_gap_seconds())

    def gap_ready(self) -> bool:
        """True if the inter-action gap since the last application has elapsed."""
        until = self.store.get("li:next_action_at")
        return not until or self._epoch() >= int(until)

    def gap_remaining_s(self) -> int:
        until = self.store.get("li:next_action_at")
        return max(0, int(until) - self._epoch()) if until else 0

    def can_apply_linkedin(self) -> tuple[bool, str]:
        """can_act() plus the inter-action gap — the gate for LinkedIn Easy
        Apply specifically. Kept separate from can_act() so WhatsApp/email
        outbound and discovery (which share can_act()) are not throttled by
        the LinkedIn apply gap."""
        ok, reason = self.can_act()
        if not ok:
            return ok, reason
        if not self.gap_ready():
            return False, f"min gap not elapsed ({self.gap_remaining_s()}s left)"
        return True, "ok"

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
        """Compare the wall-clock hour in the operator's timezone, not UTC.

        The signed autopilot policy expresses its window in Asia/Jerusalem, so
        evaluating it in UTC both refuses policy-allowed sends and permits
        sends the policy forbids. A mistyped timezone degrades to the previous
        UTC behaviour rather than raising inside ``can_act()``.
        """
        start, end = self.s.active_hours_range()
        now = self._now()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        try:
            local = now.astimezone(ZoneInfo(self.s.active_hours_timezone))
        except Exception:
            local = now.astimezone(UTC)
        return start <= local.hour < end

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

    def reserve_final_action(
        self,
        *,
        reservation_id: str,
        platform: str,
    ) -> tuple[bool, str]:
        """Atomically reserve one final action before the ambiguity boundary.

        A reservation is idempotent for one attempt. LinkedIn reservations
        also consume the daily budget and establish the minimum gap at the
        boundary, so an unknown/crashed click is still counted conservatively.
        """

        if not reservation_id or len(reservation_id) > 128:
            return False, "invalid reservation"
        if not self.within_active_hours():
            return False, "outside active hours"

        reservation_key = f"submit:reservation:{reservation_id}"
        if hasattr(self.store, "pipeline"):
            return self._reserve_shared(
                reservation_key=reservation_key,
                platform=platform,
            )
        with self._reservation_lock:
            if self.store.get(reservation_key):
                allowed, reason = self._reservation_stop_policy()
                if not allowed:
                    return False, reason
                return True, "already reserved"
            allowed, reason = self._reservation_policy(platform)
            if not allowed:
                return False, reason
            self.store.set(reservation_key, 1, ex=24 * 3600)
            if platform == "linkedin":
                self.store.incr(self._day_key())
                self.store.set(
                    "li:next_action_at",
                    self._epoch() + self.next_gap_seconds(),
                )
            return True, "reserved"

    def _reservation_policy(self, platform: str) -> tuple[bool, str]:
        allowed, reason = self._reservation_stop_policy()
        if not allowed:
            return False, reason
        if platform == "linkedin":
            if self.budget_remaining() <= 0:
                return False, "daily cap reached"
            if not self.gap_ready():
                return False, f"min gap not elapsed ({self.gap_remaining_s()}s left)"
        return True, "ok"

    def _reservation_stop_policy(self) -> tuple[bool, str]:
        """Recheck mutable operator stops even for an existing reservation."""
        if self.is_killed():
            return False, "kill switch active"
        if self.in_cooldown():
            return False, "in challenge cooldown"
        return True, "ok"

    def _reserve_shared(
        self,
        *,
        reservation_key: str,
        platform: str,
    ) -> tuple[bool, str]:
        from redis.exceptions import WatchError

        watched = [
            reservation_key,
            "li:kill",
            "li:cooldown_until",
        ]
        if platform == "linkedin":
            watched.extend([self._day_key(), "li:next_action_at"])

        for _ in range(5):
            with self.store.pipeline() as pipe:
                try:
                    pipe.watch(*watched)
                    killed = pipe.get("li:kill")
                    if killed in {b"1", "1"}:
                        return False, "kill switch active"
                    cooldown_until = int(pipe.get("li:cooldown_until") or 0)
                    if self._epoch() < cooldown_until:
                        return False, "in challenge cooldown"
                    if pipe.get(reservation_key):
                        return True, "already reserved"
                    if platform == "linkedin":
                        used = int(pipe.get(self._day_key()) or 0)
                        if used >= self.s.linkedin_daily_cap:
                            return False, "daily cap reached"
                        next_action = int(pipe.get("li:next_action_at") or 0)
                        if self._epoch() < next_action:
                            return False, "min gap not elapsed"

                    pipe.multi()
                    pipe.set(reservation_key, 1, ex=24 * 3600)
                    if platform == "linkedin":
                        pipe.incr(self._day_key())
                        pipe.set(
                            "li:next_action_at",
                            self._epoch() + self.next_gap_seconds(),
                        )
                    pipe.execute()
                    return True, "reserved"
                except WatchError:
                    continue
        return False, "governor contention"

    # ── circuit breaker (cooldown) ────────────────────
    def _epoch(self) -> int:
        n = self._now()
        return int(n.timestamp()) if hasattr(n, "timestamp") else 0

    def trip_cooldown(self) -> int:
        """Record a challenge and set/extend cooldown. Returns hours applied."""
        window_key = "li:trips"
        trips = int(self.store.get(window_key) or 0)
        hours = min(48, 6 * (2**trips))
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
            "gap_remaining_s": self.gap_remaining_s(),
        }


_governor: RateGovernor | None = None


def get_governor(*, require_shared: bool = False) -> RateGovernor:
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
    if require_shared and not _governor.shared_backend:
        raise GovernorUnavailableError("shared governor backend unavailable")
    return _governor
