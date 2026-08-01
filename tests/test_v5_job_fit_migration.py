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
REVISION_016 = "016_discovery_mesh"


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


def test_job_fit_migration_round_trip_preserves_existing_applications(tmp_path):
    database = tmp_path / "job-fit-migration.db"
    _alembic(database, "upgrade", REVISION_016)
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
                "INSERT INTO applications (job_id, status, created_at, updated_at) "
                "VALUES (1, 'draft', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    _alembic(database, "upgrade", "head")
    engine = create_engine(_database_url(database))
    inspector = inspect(engine)
    assert "job_fit_decisions" in inspector.get_table_names()
    application_columns = {column["name"] for column in inspector.get_columns("applications")}
    assert {"cv_routing_margin", "job_fit_decision_id"}.issubset(application_columns)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM applications")).scalar_one() == 1
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
        if "job_fit_decisions" in repr(difference)
        or "cv_routing_margin" in repr(difference)
        or "job_fit_decision_id" in repr(difference)
    ]
    assert relevant == []
    engine.dispose()

    _alembic(database, "downgrade", REVISION_016)
    engine = create_engine(_database_url(database))
    assert "job_fit_decisions" not in inspect(engine).get_table_names()
    application_columns = {column["name"] for column in inspect(engine).get_columns("applications")}
    assert {"cv_routing_margin", "job_fit_decision_id"}.isdisjoint(application_columns)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM applications")).scalar_one() == 1
    engine.dispose()
