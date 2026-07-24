import random
from core.config import Settings
from core.governor import RateGovernor


class Clock:
    def __init__(self, epoch=1_000_000): self.t = epoch
    def now(self):
        import datetime as d
        return d.datetime.utcfromtimestamp(self.t)


def _gov(clock):
    s = Settings(_env_file=None)
    return RateGovernor(s, redis_client=None, now_fn=clock.now, rng=random.Random(1))


def test_first_trip_is_six_hours_and_blocks():
    c = Clock(); gov = _gov(c)
    hours = gov.trip_cooldown()
    assert hours == 6
    assert gov.in_cooldown() is True
    c.t += 6 * 3600 + 1
    assert gov.in_cooldown() is False


def test_cooldown_doubles_on_repeat():
    c = Clock(); gov = _gov(c)
    assert gov.trip_cooldown() == 6
    c.t += 60
    assert gov.trip_cooldown() == 12
    c.t += 60
    assert gov.trip_cooldown() == 24
