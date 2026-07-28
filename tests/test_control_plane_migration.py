"""Upgrade/downgrade evidence for the local control-plane bridge schema."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from db.models import Base

ROOT = Path(__file__).resolve().parents[1]
TABLES = {
    "control_plane_application_refs",
    "control_plane_review_grants",
    "control_plane_command_receipts",
    "control_plane_event_outbox",
}


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


def test_control_plane_migration_round_trip_and_metadata_match(tmp_path):
    database = tmp_path / "control-plane-migration.db"
    _alembic(database, "upgrade", "012_smartrecruiters_disclosures")
    engine = create_engine(_database_url(database))
    assert TABLES.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()

    _alembic(database, "upgrade", "head")
    engine = create_engine(_database_url(database))
    assert TABLES.issubset(inspect(engine).get_table_names())
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        relevant_differences = []
        for group in compare_metadata(context, Base.metadata):
            differences = group if isinstance(group, list) else [group]
            for difference in differences:
                operation = difference[0]
                if operation in {"add_table", "remove_table"}:
                    table_name = difference[1].name
                else:
                    table_name = difference[2]
                if table_name in TABLES:
                    relevant_differences.append(difference)
        # Existing SQLite enum rendering and downgrade archives are unrelated;
        # all four new live tables must match their ORM contract exactly.
        assert relevant_differences == []
    engine.dispose()

    _alembic(database, "downgrade", "012_smartrecruiters_disclosures")
    engine = create_engine(_database_url(database))
    assert TABLES.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()

    _alembic(database, "upgrade", "head")
    engine = create_engine(_database_url(database))
    assert TABLES.issubset(inspect(engine).get_table_names())
    engine.dispose()
