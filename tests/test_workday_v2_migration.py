"""Upgrade/downgrade evidence for Workday browser qualification revision 011."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
REVISION_010 = "010_ollama_form_plan_v1"
REVISION_011 = "011_workday_browser_v2"
BASE_COLUMNS = (
    "id",
    "selector_version",
    "terminal_reason",
    "qualified",
    "trace_json",
    "created_at",
)


def _url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _alembic(database_url: str, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _base_snapshot(connection, record_id: int) -> dict[str, object]:
    return dict(
        connection.execute(
            text(
                f"SELECT {', '.join(BASE_COLUMNS)} "
                "FROM browser_qualification_runs WHERE id = :record_id"
            ),
            {"record_id": record_id},
        )
        .mappings()
        .one()
    )


def _insert_legacy_row(connection, *, record_id: int) -> None:
    connection.execute(
        text(
            "INSERT INTO browser_qualification_runs "
            "(id, selector_version, terminal_reason, qualified, trace_json, created_at) "
            "VALUES (:id, 'legacy-selector', 'DRY_RUN_DISCARDED', true, '[]', "
            "'2026-07-26 00:00:00')"
        ),
        {"id": record_id},
    )


def _insert_v2_row(
    connection,
    *,
    record_id: int,
    tier: str = "fixture_qualified",
    qualified: bool = True,
    terminal_reason: str = "FIXTURE_SUITE_PASSED",
    form_fingerprint: str | None = None,
    fixture_digest: str = "a" * 64,
) -> None:
    connection.execute(
        text(
            "INSERT INTO browser_qualification_runs "
            "(id, selector_version, terminal_reason, qualified, trace_json, "
            "adapter_name, adapter_version, qualification_tier, form_fingerprint, "
            "fixture_digest, created_at) VALUES "
            "(:id, 'workday-candidate-v2', :terminal_reason, :qualified, '[]', "
            "'workday', '2.0.0', :tier, :form_fingerprint, :fixture_digest, "
            "'2026-07-26 01:00:00')"
        ),
        {
            "id": record_id,
            "terminal_reason": terminal_reason,
            "qualified": qualified,
            "tier": tier,
            "form_fingerprint": form_fingerprint,
            "fixture_digest": fixture_digest,
        },
    )


def test_revision_011_preserves_legacy_rows_and_is_reversible(tmp_path):
    database_url = _url(tmp_path / "workday-v2-migration.db")
    _alembic(database_url, "upgrade", REVISION_010)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        _insert_legacy_row(connection, record_id=1)
        connection.execute(
            text(
                "INSERT INTO operator_approved_answers "
                "(id, canonical_field, field_type, option_set_hash, locale, "
                "profile_version, selected_cv_id, selected_cv_hash, adapter_name, "
                "adapter_version, selector_version, form_fingerprint, policy_version, "
                "answer_json, evidence_source, evidence_reference, approved_by, "
                "approved_at, created_at) VALUES "
                "(1, 'phone', 'phone', :digest, 'en', 1, 'fixture-cv', :digest, "
                "'workday', '2.0.0', 'workday-candidate-v2', :digest, "
                "'answer-policy-v1', '\"+15551234567\"', 'operator_confirmation', "
                "'legacy-fixture', 'fixture-operator', "
                "'2026-07-26 00:00:00', '2026-07-26 00:00:00')"
            ),
            {"digest": "a" * 64},
        )
    with engine.connect() as connection:
        legacy_snapshot = _base_snapshot(connection, 1)
    engine.dispose()

    _alembic(database_url, "upgrade", REVISION_011)
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert {
        "attachment_verification_source",
        "attachment_verified_at",
    }.issubset({column["name"] for column in inspector.get_columns("form_plans")})
    assert "field_contract_fingerprint" in {
        column["name"] for column in inspector.get_columns("operator_approved_answers")
    }
    assert "ix_operator_approved_answers_field_contract" in {
        index["name"] for index in inspector.get_indexes("operator_approved_answers")
    }
    assert {
        "adapter_name",
        "adapter_version",
        "qualification_tier",
        "form_fingerprint",
        "fixture_digest",
    }.issubset({column["name"] for column in inspector.get_columns("browser_qualification_runs")})
    assert {
        "ix_browser_qualification_adapter_tier",
        "ix_browser_qualification_adapter_form",
    }.issubset({index["name"] for index in inspector.get_indexes("browser_qualification_runs")})
    with engine.connect() as connection:
        assert _base_snapshot(connection, 1) == legacy_snapshot
        metadata = (
            connection.execute(
                text(
                    "SELECT adapter_name, adapter_version, qualification_tier, "
                    "form_fingerprint, fixture_digest "
                    "FROM browser_qualification_runs WHERE id = 1"
                )
            )
            .mappings()
            .one()
        )
        assert all(value is None for value in metadata.values())
        assert (
            connection.execute(
                text(
                    "SELECT field_contract_fingerprint FROM operator_approved_answers WHERE id = 1"
                )
            ).scalar_one()
            is None
        )

    with engine.begin() as connection:
        _insert_v2_row(connection, record_id=2)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_v2_row(connection, record_id=3, tier="unreviewed")

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_v2_row(connection, record_id=4, fixture_digest="not-a-digest")

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO browser_qualification_runs "
                    "(id, selector_version, terminal_reason, qualified, trace_json, "
                    "adapter_name) VALUES "
                    "(4, 'workday-candidate-v2', 'FIXTURE_SUITE_PASSED', "
                    "true, '[]', 'workday')"
                )
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            _insert_v2_row(
                connection,
                record_id=5,
                tier="live_canary_qualified",
                terminal_reason="FIXTURE_SUITE_PASSED",
                form_fingerprint="f" * 64,
            )
    engine.dispose()

    _alembic(database_url, "downgrade", REVISION_010)
    engine = create_engine(database_url)
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("browser_qualification_runs")}
    assert "qualification_tier" not in columns
    form_plan_columns = {column["name"] for column in inspector.get_columns("form_plans")}
    assert "attachment_verification_source" not in form_plan_columns
    assert "attachment_verified_at" not in form_plan_columns
    operator_columns = {
        column["name"] for column in inspector.get_columns("operator_approved_answers")
    }
    assert "field_contract_fingerprint" not in operator_columns
    with engine.connect() as connection:
        assert _base_snapshot(connection, 1) == legacy_snapshot
        assert (
            connection.execute(text("SELECT count(*) FROM browser_qualification_runs")).scalar_one()
            == 2
        )
        assert (
            connection.execute(text("SELECT count(*) FROM operator_approved_answers")).scalar_one()
            == 1
        )
    engine.dispose()

    _alembic(database_url, "upgrade", REVISION_011)
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert {
        "attachment_verification_source",
        "attachment_verified_at",
    }.issubset({column["name"] for column in inspector.get_columns("form_plans")})
    assert "field_contract_fingerprint" in {
        column["name"] for column in inspector.get_columns("operator_approved_answers")
    }
    with engine.connect() as connection:
        assert _base_snapshot(connection, 1) == legacy_snapshot
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM browser_qualification_runs "
                    "WHERE adapter_name IS NULL AND qualification_tier IS NULL"
                )
            ).scalar_one()
            == 2
        )
        assert (
            connection.execute(
                text(
                    "SELECT field_contract_fingerprint FROM operator_approved_answers WHERE id = 1"
                )
            ).scalar_one()
            is None
        )
    engine.dispose()
