"""Round-trip and ORM parity coverage for operational metric storage."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import BigInteger, create_engine, inspect

from db.models import Base

ROOT = Path(__file__).resolve().parents[1]
TABLES = {
    "operational_metric_receipts",
    "operational_metric_events",
    "operational_metric_rollups",
}
ROLLUP_COUNTERS = {
    "event_count",
    "duration_count",
    "duration_sum_ms",
    "duration_le_1s",
    "duration_le_5s",
    "duration_le_15s",
    "duration_le_60s",
    "duration_le_300s",
    "duration_le_900s",
    "duration_le_inf",
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


def test_operational_metrics_migration_round_trip_and_metadata_parity(tmp_path):
    database = tmp_path / "operational-metrics-migration.db"
    _alembic(database, "upgrade", "014_control_grant_revocations")
    engine = create_engine(_database_url(database))
    assert TABLES.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()

    _alembic(database, "upgrade", "head")
    engine = create_engine(_database_url(database))
    inspector = inspect(engine)
    assert TABLES.issubset(inspector.get_table_names())

    for table_name in TABLES:
        table = Base.metadata.tables[table_name]
        expected_indexes = {index.name for index in table.indexes}
        actual_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
        assert expected_indexes == actual_indexes
        expected_checks = {
            constraint.name
            for constraint in table.constraints
            if constraint.__class__.__name__ == "CheckConstraint"
        }
        actual_checks = {
            constraint["name"] for constraint in inspector.get_check_constraints(table_name)
        }
        assert expected_checks == actual_checks
        expected_uniques = {
            constraint.name
            for constraint in table.constraints
            if constraint.__class__.__name__ == "UniqueConstraint"
        }
        actual_uniques = {
            constraint["name"] for constraint in inspector.get_unique_constraints(table_name)
        }
        assert expected_uniques == actual_uniques
        expected_foreign_keys = {constraint.name for constraint in table.foreign_key_constraints}
        actual_foreign_keys = {
            constraint["name"] for constraint in inspector.get_foreign_keys(table_name)
        }
        assert expected_foreign_keys == actual_foreign_keys

    receipt_columns = {
        column["name"] for column in inspector.get_columns("operational_metric_receipts")
    }
    assert receipt_columns == {"event_key", "recorded_at"}
    rollup_columns = {
        column["name"]: column["type"]
        for column in inspector.get_columns("operational_metric_rollups")
    }
    assert all(
        isinstance(Base.metadata.tables["operational_metric_rollups"].c[name].type, BigInteger)
        for name in ROLLUP_COUNTERS
    )
    assert all("BIGINT" in str(rollup_columns[name]).upper() for name in ROLLUP_COUNTERS)

    with engine.connect() as connection:
        differences = compare_metadata(
            MigrationContext.configure(
                connection,
                opts={"compare_type": False, "compare_server_default": True},
            ),
            Base.metadata,
        )
    relevant = []
    for group in differences:
        candidates = group if isinstance(group, list) else [group]
        for difference in candidates:
            rendered = repr(difference)
            if any(table_name in rendered for table_name in TABLES):
                relevant.append(difference)
    assert relevant == []
    engine.dispose()

    _alembic(database, "downgrade", "014_control_grant_revocations")
    engine = create_engine(_database_url(database))
    assert TABLES.isdisjoint(inspect(engine).get_table_names())
    engine.dispose()
