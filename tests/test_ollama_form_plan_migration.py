"""Upgrade/downgrade evidence for form-planning persistence revision 010."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
REVISION_009 = "009_submission_domain_kernel"
REVISION_010 = "010_ollama_form_plan_v1"
ARCHIVE_TABLE = "_operator_approved_answers_010_archive"
OPERATOR_SNAPSHOT_COLUMNS = (
    "id",
    "canonical_field",
    "field_type",
    "option_set_hash",
    "locale",
    "profile_version",
    "selected_cv_id",
    "selected_cv_hash",
    "adapter_name",
    "adapter_version",
    "selector_version",
    "form_fingerprint",
    "policy_version",
    "answer_json",
    "evidence_source",
    "evidence_reference",
    "approved_by",
    "approved_at",
    "revoked_at",
    "revoked_by",
    "revocation_reason",
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


def _seed_revision_009(
    connection: Connection,
    *,
    record_id: int,
) -> None:
    plan_id = f"00000000-0000-4000-8000-{record_id:012d}"
    connection.execute(
        text(
            "INSERT INTO jobs (id, title, source_url, status, created_at) "
            "VALUES (:record_id, 'Seeded Engineer', :source_url, 'draft', "
            "'2026-07-26 00:00:00')"
        ),
        {
            "record_id": record_id,
            "source_url": f"https://example.test/{record_id}",
        },
    )
    connection.execute(
        text(
            "INSERT INTO applications "
            "(id, job_id, status, created_at, updated_at, revision) "
            "VALUES (:record_id, :record_id, 'draft', "
            "'2026-07-26 00:00:00', '2026-07-26 00:00:00', 1)"
        ),
        {"record_id": record_id},
    )
    connection.execute(
        text(
            "INSERT INTO form_plans "
            "(id, plan_id, application_id, application_revision, adapter_name, "
            "adapter_version, selector_version, fingerprint, selected_cv_id, "
            "selected_cv_hash, attachment_verified, fields_json, decisions_json, "
            "blockers_json, created_at, expires_at) VALUES "
            "(:record_id, :plan_id, :record_id, 1, 'fixture', '1.0.0', "
            "'fixture-v1', :fingerprint, 'cv', :cv_hash, false, '[]', '[]', "
            "'[]', '2026-07-26 00:00:00', '2026-07-26 00:30:00')"
        ),
        {
            "record_id": record_id,
            "plan_id": plan_id,
            "fingerprint": "f" * 64,
            "cv_hash": "c" * 64,
        },
    )


def _insert_operator_answer(
    connection: Connection,
    *,
    record_id: int,
) -> None:
    connection.execute(
        text(
            "INSERT INTO operator_approved_answers "
            "(id, canonical_field, field_type, option_set_hash, locale, "
            "profile_version, selected_cv_id, selected_cv_hash, adapter_name, "
            "adapter_version, selector_version, form_fingerprint, policy_version, "
            "answer_json, evidence_source, evidence_reference, approved_by, "
            "approved_at, revoked_at, revoked_by, revocation_reason, created_at) "
            "VALUES (:record_id, 'primary_language', 'text', :option_hash, 'en', "
            "1, 'cv', :cv_hash, 'fixture', '1.0.0', 'fixture-v1', :fingerprint, "
            "'answer-policy-v1', :answer_json, 'operator_confirmation', "
            ":evidence_reference, 'operator', '2026-07-26 01:00:00', "
            "'2026-07-26 02:00:00', 'operator', 'superseded', "
            "'2026-07-26 01:00:00')"
        ),
        {
            "record_id": record_id,
            "option_hash": "0" * 64,
            "cv_hash": "c" * 64,
            "fingerprint": "f" * 64,
            "answer_json": '"Python"',
            "evidence_reference": f"review-{record_id}",
        },
    )


def _operator_answer_snapshot(
    connection: Connection,
    *,
    record_id: int,
) -> dict[str, object]:
    columns = ", ".join(OPERATOR_SNAPSHOT_COLUMNS)
    return dict(
        connection.execute(
            text(f"SELECT {columns} FROM operator_approved_answers WHERE id = :record_id"),
            {"record_id": record_id},
        )
        .mappings()
        .one()
    )


def _assert_seeded_domain_records(connection: Connection, *, record_id: int) -> None:
    assert (
        connection.execute(
            text("SELECT count(*) FROM jobs WHERE id = :record_id"),
            {"record_id": record_id},
        ).scalar_one()
        == 1
    )
    assert (
        connection.execute(
            text("SELECT count(*) FROM applications WHERE id = :record_id"),
            {"record_id": record_id},
        ).scalar_one()
        == 1
    )
    assert (
        connection.execute(
            text("SELECT count(*) FROM form_plans WHERE id = :record_id"),
            {"record_id": record_id},
        ).scalar_one()
        == 1
    )


def _assert_operator_answer_restored(
    connection: Connection,
    *,
    record_id: int,
    expected_snapshot: dict[str, object],
) -> None:
    assert (
        _operator_answer_snapshot(
            connection,
            record_id=record_id,
        )
        == expected_snapshot
    )
    assert not inspect(connection).has_table(ARCHIVE_TABLE)


def test_revision_010_preserves_old_plans_and_is_reversible(tmp_path):
    database = tmp_path / "form-plan-010.db"
    database_url = _url(database)
    _alembic(database_url, "upgrade", REVISION_009)

    engine = create_engine(database_url)
    with engine.begin() as connection:
        _seed_revision_009(connection, record_id=1)
    engine.dispose()

    _alembic(database_url, "upgrade", REVISION_010)
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "operator_approved_answers" in inspector.get_table_names()
    assert "selected_cv_hash" in {
        column["name"] for column in inspector.get_columns("applications")
    }
    with engine.connect() as connection:
        _assert_seeded_domain_records(connection, record_id=1)
        row = (
            connection.execute(
                text("SELECT locale, answer_policy_version FROM form_plans WHERE id = 1")
            )
            .mappings()
            .one()
        )
        assert dict(row) == {
            "locale": "en",
            "answer_policy_version": "legacy-unverified",
        }

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE applications SET material_eligible = true, "
                    "selected_cv_hash = :cv_hash WHERE id = 1"
                ),
                {"cv_hash": "c" * 64},
            )

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO operator_approved_answers "
                    "(canonical_field, field_type, option_set_hash, locale, "
                    "profile_version, selected_cv_id, selected_cv_hash, adapter_name, "
                    "adapter_version, selector_version, form_fingerprint, policy_version, "
                    "answer_json, evidence_source, evidence_reference, approved_by) "
                    "VALUES ('field', 'text', 'bad', 'en', 1, 'cv', 'bad', 'fixture', "
                    "'1.0.0', 'fixture-v1', 'bad', 'answer-policy-v1', '\"x\"', "
                    "'operator_confirmation', 'review-1', 'operator')"
                )
            )

    with engine.begin() as connection:
        _insert_operator_answer(connection, record_id=7)
    with engine.connect() as connection:
        expected_operator_answer = _operator_answer_snapshot(connection, record_id=7)
    engine.dispose()

    _alembic(database_url, "downgrade", REVISION_009)
    engine = create_engine(database_url)
    inspector = inspect(engine)
    assert "operator_approved_answers" not in inspector.get_table_names()
    assert ARCHIVE_TABLE in inspector.get_table_names()
    assert "locale" not in {column["name"] for column in inspector.get_columns("form_plans")}
    with engine.connect() as connection:
        _assert_seeded_domain_records(connection, record_id=1)
        assert connection.execute(text(f"SELECT count(*) FROM {ARCHIVE_TABLE}")).scalar_one() == 1
    engine.dispose()

    _alembic(database_url, "upgrade", REVISION_010)
    engine = create_engine(database_url)
    with engine.connect() as connection:
        _assert_seeded_domain_records(connection, record_id=1)
        _assert_operator_answer_restored(
            connection,
            record_id=7,
            expected_snapshot=expected_operator_answer,
        )
    engine.dispose()


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL migration preservation requires the CI service database",
)
def test_revision_010_preserves_seeded_postgres_records_across_cycle():
    source_url = make_url(os.environ["DATABASE_URL"])
    database_name = f"job_agent_migration_{uuid4().hex}"
    admin_url = source_url.set(database="postgres").render_as_string(hide_password=False)
    database_url = source_url.set(database=database_name).render_as_string(hide_password=False)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    engine = None
    database_created = False

    try:
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
        database_created = True

        _alembic(database_url, "upgrade", REVISION_009)
        engine = create_engine(database_url)
        with engine.begin() as connection:
            _seed_revision_009(connection, record_id=101)
        engine.dispose()
        engine = None

        _alembic(database_url, "upgrade", REVISION_010)
        engine = create_engine(database_url)
        with engine.begin() as connection:
            _assert_seeded_domain_records(connection, record_id=101)
            assert (
                connection.execute(
                    text("SELECT answer_policy_version FROM form_plans WHERE id = 101")
                ).scalar_one()
                == "legacy-unverified"
            )
            _insert_operator_answer(connection, record_id=101)
        with engine.connect() as connection:
            expected_operator_answer = _operator_answer_snapshot(
                connection,
                record_id=101,
            )
        engine.dispose()
        engine = None

        _alembic(database_url, "downgrade", REVISION_009)
        engine = create_engine(database_url)
        with engine.connect() as connection:
            _assert_seeded_domain_records(connection, record_id=101)
            assert not inspect(connection).has_table("operator_approved_answers")
            assert inspect(connection).has_table(ARCHIVE_TABLE)
            assert (
                connection.execute(text(f"SELECT count(*) FROM {ARCHIVE_TABLE}")).scalar_one() == 1
            )
        engine.dispose()
        engine = None

        _alembic(database_url, "upgrade", REVISION_010)
        engine = create_engine(database_url)
        with engine.begin() as connection:
            _assert_seeded_domain_records(connection, record_id=101)
            _assert_operator_answer_restored(
                connection,
                record_id=101,
                expected_snapshot=expected_operator_answer,
            )
            next_id = connection.execute(
                text(
                    "INSERT INTO operator_approved_answers "
                    "(canonical_field, field_type, option_set_hash, locale, "
                    "profile_version, selected_cv_id, selected_cv_hash, adapter_name, "
                    "adapter_version, selector_version, form_fingerprint, policy_version, "
                    "answer_json, evidence_source, evidence_reference, approved_by) "
                    "VALUES ('secondary_language', 'text', :option_hash, 'en', 1, "
                    "'cv', :cv_hash, 'fixture', '1.0.0', 'fixture-v1', :fingerprint, "
                    "'answer-policy-v1', :answer_json, 'operator_confirmation', "
                    "'review-next', 'operator') RETURNING id"
                ),
                {
                    "option_hash": "1" * 64,
                    "cv_hash": "c" * 64,
                    "fingerprint": "f" * 64,
                    "answer_json": '"Rust"',
                },
            ).scalar_one()
            assert next_id > 101
        engine.dispose()
        engine = None
    finally:
        if engine is not None:
            engine.dispose()
        if database_created:
            with admin_engine.connect() as connection:
                connection.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :database_name AND pid <> pg_backend_pid()"
                    ),
                    {"database_name": database_name},
                )
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin_engine.dispose()
