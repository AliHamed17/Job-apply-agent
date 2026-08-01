"""Upgrade/downgrade evidence for signed qualified-autopilot authority."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from db.models import Base

ROOT = Path(__file__).resolve().parents[1]
REVISION_017 = "017_job_fit_decisions"


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _alembic(path: Path, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = _database_url(path)
    env["APP_ENV"] = "test"
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_signed_autopilot_migration_round_trip_preserves_existing_history(tmp_path) -> None:
    database = tmp_path / "signed-autopilot-migration.db"
    _alembic(database, "upgrade", REVISION_017)
    engine = create_engine(_database_url(database))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs (title, source_url, status, created_at) "
                "VALUES ('Existing', 'https://example.test/existing', "
                "'draft', CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO applications (job_id, status, revision, created_at, updated_at) "
                "VALUES (1, 'draft', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO browser_qualification_runs "
                "(selector_version, terminal_reason, qualified, trace_json, adapter_name, "
                "adapter_version, qualification_tier, form_fingerprint, fixture_digest, "
                "created_at) VALUES "
                "('legacy-live-v1', 'LIVE_CANARY_CONFIRMED', true, '[]', 'greenhouse', "
                "'1.0.0', 'live_canary_qualified', :fingerprint, :fixture_digest, "
                "CURRENT_TIMESTAMP)"
            ),
            {"fingerprint": "f" * 64, "fixture_digest": "e" * 64},
        )
    engine.dispose()

    _alembic(database, "upgrade", "head")
    engine = create_engine(_database_url(database))
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "automation_policy_revisions",
        "autopilot_inspection_runs",
        "application_policy_decisions",
        "automation_kill_switch_events",
    }.issubset(tables)
    submission_columns = {column["name"] for column in inspector.get_columns("submissions")}
    assert {
        "authority_kind",
        "automation_policy_decision_id",
        "automation_policy_decision_digest",
    }.issubset(submission_columns)
    permit_columns = {column["name"] for column in inspector.get_columns("final_submit_permits")}
    assert {"authority_kind", "automation_policy_decision_digest"}.issubset(permit_columns)
    qualification_columns = {
        column["name"] for column in inspector.get_columns("browser_qualification_runs")
    }
    assert "form_contract_digest" in qualification_columns
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM applications")) == 1
        legacy_canary = (
            connection.execute(
                text(
                    "SELECT qualification_tier, qualified, terminal_reason, "
                    "form_contract_digest FROM browser_qualification_runs "
                    "WHERE selector_version = 'legacy-live-v1'"
                )
            )
            .mappings()
            .one()
        )
        assert legacy_canary["qualification_tier"] == "dry_run_qualified"
        assert bool(legacy_canary["qualified"]) is True
        assert legacy_canary["terminal_reason"] == "LIVE_CANARY_CONFIRMED"
        assert legacy_canary["form_contract_digest"] is None
        differences = compare_metadata(
            MigrationContext.configure(
                connection,
                opts={"compare_type": False, "compare_server_default": True},
            ),
            Base.metadata,
        )
    relevant = [
        difference
        for difference in differences
        if any(
            marker in repr(difference)
            for marker in (
                "automation_policy_revisions",
                "autopilot_inspection_runs",
                "application_policy_decisions",
                "automation_kill_switch_events",
                "authority_kind",
                "automation_policy_decision",
                "form_contract_digest",
            )
        )
    ]
    assert relevant == []
    engine.dispose()

    _alembic(database, "downgrade", REVISION_017)
    engine = create_engine(_database_url(database))
    inspector = inspect(engine)
    assert {
        "automation_policy_revisions",
        "autopilot_inspection_runs",
        "application_policy_decisions",
        "automation_kill_switch_events",
    }.isdisjoint(inspector.get_table_names())
    submission_columns = {column["name"] for column in inspector.get_columns("submissions")}
    assert {
        "authority_kind",
        "automation_policy_decision_id",
        "automation_policy_decision_digest",
    }.isdisjoint(submission_columns)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM applications")) == 1
    engine.dispose()

    _alembic(database, "upgrade", "head")
