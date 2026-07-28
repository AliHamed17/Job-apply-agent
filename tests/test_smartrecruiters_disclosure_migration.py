"""Upgrade/downgrade preservation for disclosure revision 012."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]
REVISION_011 = "011_workday_browser_v2"
REVISION_012 = "012_smartrecruiters_disclosures"


def _url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _alembic(database_url: str, *args: str) -> None:
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def _insert_legacy_plan(connection) -> None:
    connection.execute(
        text(
            "INSERT INTO form_plans "
            "(id, plan_id, application_id, application_revision, adapter_name, "
            "adapter_version, selector_version, fingerprint, selected_cv_id, "
            "selected_cv_hash, attachment_verified, fields_json, decisions_json, "
            "blockers_json, locale, answer_policy_version, created_at, expires_at) "
            "VALUES "
            "(1, '00000000-0000-4000-8000-000000000001', 1, 1, "
            "'smartrecruiters', 'legacy', 'legacy', :digest, 'fixture-cv', "
            ":digest, false, '[]', '[]', '[]', 'en', 'answer-policy-v1', "
            "'2026-07-27 00:00:00', '2026-07-27 00:30:00')"
        ),
        {"digest": "a" * 64},
    )


def test_revision_012_defaults_legacy_rows_and_is_reversible(tmp_path) -> None:
    database_url = _url(tmp_path / "smartrecruiters-disclosures.db")
    _alembic(database_url, "upgrade", REVISION_011)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _insert_legacy_plan(connection)
    engine.dispose()

    _alembic(database_url, "upgrade", REVISION_012)
    engine = create_engine(database_url)
    columns = {column["name"] for column in inspect(engine).get_columns("form_plans")}
    assert "disclosures_json" in columns
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT disclosures_json FROM form_plans WHERE id = 1")
            ).scalar_one()
            == "[]"
        )
        connection.close()
    engine.dispose()

    _alembic(database_url, "downgrade", REVISION_011)
    engine = create_engine(database_url)
    columns = {column["name"] for column in inspect(engine).get_columns("form_plans")}
    assert "disclosures_json" not in columns
    with engine.connect() as connection:
        assert (
            connection.execute(text("SELECT plan_id FROM form_plans WHERE id = 1")).scalar_one()
            == "00000000-0000-4000-8000-000000000001"
        )
    engine.dispose()

    _alembic(database_url, "upgrade", REVISION_012)
    engine = create_engine(database_url)
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT disclosures_json FROM form_plans WHERE id = 1")
            ).scalar_one()
            == "[]"
        )
    engine.dispose()
