from core.config import Settings


def test_new_defaults_present():
    s = Settings(_env_file=None)
    assert s.min_apply_score == 40.0
    assert s.linkedin_daily_cap == 45
    assert s.linkedin_min_gap_s == 120
    assert s.linkedin_max_gap_s == 360
    assert s.discovery_interval_h == 3
    assert s.wa_outbound_daily_cap == 15
    assert s.wa_contact_dedup_days == 30
    assert s.dry_run is False


def test_active_hours_range_parses():
    s = Settings(_env_file=None, active_hours="09:00-21:00")
    assert s.active_hours_range() == (9, 21)
