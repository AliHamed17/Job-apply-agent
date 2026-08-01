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
TABLES = {
    "search_intent_revisions",
    "discovery_sources",
    "discovery_cursors",
    "employer_catalog_entries",
    "job_source_occurrences",
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


def test_discovery_mesh_migration_backfills_and_round_trips(tmp_path):
    database = tmp_path / "discovery-mesh-migration.db"
    _alembic(database, "upgrade", "015_operational_metrics")
    engine = create_engine(_database_url(database))
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO jobs "
                "(title, company, location, apply_url, source_url, "
                "apply_url_hash, job_signature, status, discovery_source, created_at) "
                "VALUES "
                "('ML Engineer', 'Example', 'Israel', "
                "'https://example.test/jobs/1', 'https://example.test/jobs/1', "
                ":url_digest, :signature, 'scored', 'remotive', CURRENT_TIMESTAMP)"
            ),
            {"url_digest": "legacy-invalid", "signature": "a" * 64},
        )
    engine.dispose()

    _alembic(database, "upgrade", "head")
    engine = create_engine(_database_url(database))
    inspector = inspect(engine)
    assert TABLES.issubset(inspector.get_table_names())
    assert {"updated", "duplicates", "closed"}.issubset(
        {column["name"] for column in inspector.get_columns("discovery_runs")}
    )
    with engine.connect() as connection:
        occurrence = connection.execute(
            text(
                "SELECT source_key, normalized_url, normalized_url_hash, active "
                "FROM job_source_occurrences"
            )
        ).one()
        assert occurrence[0] == "remotive"
        assert occurrence[1] == "https://example.test/jobs/1"
        assert len(occurrence[2]) == 64
        assert occurrence[2] != "legacy-invalid"
        assert bool(occurrence[3]) is True
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
        if any(table_name in repr(difference) for table_name in TABLES)
    ]
    assert relevant == []
    engine.dispose()

    _alembic(database, "downgrade", "015_operational_metrics")
    engine = create_engine(_database_url(database))
    assert TABLES.isdisjoint(inspect(engine).get_table_names())
    assert {"updated", "duplicates", "closed"}.isdisjoint(
        {column["name"] for column in inspect(engine).get_columns("discovery_runs")}
    )
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 1
    engine.dispose()
