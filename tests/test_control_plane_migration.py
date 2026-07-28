"""Upgrade/downgrade evidence for the local control-plane bridge schema."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

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


def test_revocation_migration_backfills_preexisting_revoked_grants(tmp_path):
    database = tmp_path / "control-plane-revocation-backfill.db"
    _alembic(database, "upgrade", "013_vercel_local_control_plane")
    engine = create_engine(_database_url(database))
    now = datetime.now(UTC).replace(tzinfo=None)
    insert = text(
        "INSERT INTO control_plane_review_grants ("
        "grant_ref, application_id, application_ref_id, form_plan_id, "
        "application_revision, job_url_hash, form_plan_fingerprint, cv_hash, "
        "adapter_name, adapter_version, selector_version, runner_release, "
        "issued_at, expires_at, revoked_at"
        ") VALUES ("
        ":grant_ref, 1, 1, 1, 1, :digest, :digest, :digest, "
        "'greenhouse', '1.0.0', 'greenhouse-v1', :release, "
        ":issued_at, :expires_at, :revoked_at"
        ")"
    )
    with engine.begin() as connection:
        connection.execute(
            insert,
            {
                "grant_ref": "00000000-0000-4000-8000-000000000001",
                "digest": "a" * 64,
                "release": "b" * 64,
                "issued_at": now,
                "expires_at": now + timedelta(minutes=5),
                "revoked_at": now + timedelta(seconds=1),
            },
        )
        connection.execute(
            insert,
            {
                "grant_ref": "00000000-0000-4000-8000-000000000002",
                "digest": "c" * 64,
                "release": "d" * 64,
                "issued_at": now,
                "expires_at": now + timedelta(seconds=1),
                "revoked_at": now + timedelta(seconds=2),
            },
        )
    engine.dispose()

    _alembic(database, "upgrade", "head")
    engine = create_engine(_database_url(database))
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT grant_ref, revocation_state, revocation_available_at "
                "FROM control_plane_review_grants ORDER BY grant_ref"
            )
        ).all()
    assert [row.revocation_state for row in rows] == ["pending", "expired"]
    assert all(row.revocation_available_at is not None for row in rows)
    engine.dispose()

    _alembic(database, "downgrade", "013_vercel_local_control_plane")
    downgraded = create_engine(_database_url(database))
    column_names = {
        column["name"] for column in inspect(downgraded).get_columns("control_plane_review_grants")
    }
    assert "revocation_state" not in column_names
    downgraded.dispose()
