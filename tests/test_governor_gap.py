"""Regression: the LinkedIn inter-action gap (LINKEDIN_MIN_GAP_S /
MAX_GAP_S) must actually be enforced between real Easy Apply submissions —
not merely relied upon via the 5-minute beat cadence (shorter than the max
gap) or bypassed by directly-enqueued submits.

record_application() stamps a persisted next-allowed timestamp; gap_ready()
and can_apply_linkedin() gate on it. Uses a real datetime-backed clock
because the gap math goes through _epoch()/.timestamp().
"""

from __future__ import annotations

import datetime as _dt
import random

from core.config import Settings
from core.governor import RateGovernor


class _Clock:
    def __init__(self, epoch: int = 1_000_000):
        self.t = epoch

    def now(self):
        return _dt.datetime.fromtimestamp(self.t, _dt.UTC)


def _gov(clock: _Clock, **over) -> RateGovernor:
    # 13:46 UTC on the epoch below is inside the default 09:00-21:00 window.
    s = Settings(_env_file=None, linkedin_min_gap_s=120, linkedin_max_gap_s=360, **over)
    return RateGovernor(s, redis_client=None, now_fn=clock.now, rng=random.Random(1))


def test_gap_ready_before_any_application():
    gov = _gov(_Clock())
    assert gov.gap_ready() is True
    ok, _ = gov.can_apply_linkedin()
    assert ok is True


def test_gap_blocks_immediately_after_application_then_clears():
    clock = _Clock()
    gov = _gov(clock)

    gov.record_application()
    assert gov.gap_ready() is False
    ok, reason = gov.can_apply_linkedin()
    assert ok is False and "gap" in reason.lower()
    assert gov.gap_remaining_s() > 0

    # Advance the clock past the maximum possible gap — must clear.
    clock.t += 361
    assert gov.gap_ready() is True
    ok, _ = gov.can_apply_linkedin()
    assert ok is True


def test_gap_does_not_affect_shared_can_act():
    """The gap gates LinkedIn apply only; can_act() (shared with WhatsApp
    outbound + discovery) must stay True through the gap window."""
    clock = _Clock()
    gov = _gov(clock)
    gov.record_application()
    ok, _ = gov.can_act()
    assert ok is True  # can_act unaffected
    ok_li, _ = gov.can_apply_linkedin()
    assert ok_li is False  # only the LinkedIn-apply gate blocks
