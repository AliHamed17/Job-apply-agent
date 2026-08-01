"""Add tenant-scoped discovery mesh state and source occurrences.

Revision ID: 016_discovery_mesh
Revises: 015_operational_metrics
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision: str = "016_discovery_mesh"
down_revision: str | None = "015_operational_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _valid_digest(value: object) -> bool:
    candidate = str(value or "")
    return len(candidate) == 64 and all(character in "0123456789abcdef" for character in candidate)


def _datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("terminal_skip_at", sa.DateTime(), nullable=True))
    # Existing skipped rows have ambiguous provenance. Preserve them as
    # terminal rather than allowing the new discovery mesh to revive them.
    op.execute(
        sa.text(
            "UPDATE jobs SET terminal_skip_at = COALESCE(created_at, CURRENT_TIMESTAMP) "
            "WHERE status = 'skipped'"
        )
    )

    with op.batch_alter_table("discovery_runs") as batch_op:
        batch_op.add_column(sa.Column("updated", sa.Integer(), nullable=False, server_default="0"))
        batch_op.add_column(
            sa.Column("duplicates", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(sa.Column("closed", sa.Integer(), nullable=False, server_default="0"))

    op.create_table(
        "search_intent_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("version", sa.Integer(), nullable=False, unique=True),
        sa.Column(
            "schema_version",
            sa.String(32),
            nullable=False,
            server_default="search-intent.v1",
        ),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            _sha256_check("payload_digest"),
            name="ck_search_intent_revisions_digest",
        ),
    )
    op.create_index(
        "ix_search_intent_revisions_active_version",
        "search_intent_revisions",
        ["active", "version"],
    )
    op.create_index(
        "uq_search_intent_revisions_one_active",
        "search_intent_revisions",
        ["active"],
        unique=True,
        postgresql_where=sa.text("active = true"),
        sqlite_where=sa.text("active = 1"),
    )

    op.create_table(
        "discovery_sources",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_key", sa.String(255), nullable=False, unique=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("descriptor_version", sa.String(32), nullable=False),
        sa.Column(
            "configuration_digest",
            sa.String(64),
            nullable=False,
            server_default="0" * 64,
        ),
        sa.Column("transport", sa.String(32), nullable=False),
        sa.Column("authentication_mode", sa.String(32), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("cadence_seconds", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("disabled_reason", sa.String(64), nullable=True),
        sa.Column("health_status", sa.String(24), nullable=False, server_default="unknown"),
        sa.Column("next_poll_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("cadence_seconds >= 60", name="ck_discovery_sources_cadence"),
        sa.CheckConstraint(
            _sha256_check("configuration_digest"),
            name="ck_discovery_sources_configuration_digest",
        ),
        sa.CheckConstraint(
            "(enabled = true AND disabled_reason IS NULL) OR "
            "(enabled = false AND disabled_reason IS NOT NULL)",
            name="ck_discovery_sources_enabled_reason",
        ),
        sa.CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'degraded', 'disabled')",
            name="ck_discovery_sources_health",
        ),
    )
    op.create_index(
        "ix_discovery_sources_due",
        "discovery_sources",
        ["enabled", "next_poll_at"],
    )

    op.create_table(
        "employer_catalog_entries",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("catalog_key", sa.String(64), nullable=False, unique=True),
        sa.Column("company_name", sa.String(300), nullable=False),
        sa.Column("ats", sa.String(32), nullable=False),
        sa.Column("tenant_key", sa.String(255), nullable=False),
        sa.Column("region", sa.String(32), nullable=False, server_default="global"),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("discovered_via", sa.String(32), nullable=False, server_default="config"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "ats IN ('greenhouse', 'lever', 'ashby', 'smartrecruiters', "
            "'generic_jsonld', 'generic_feed')",
            name="ck_employer_catalog_entries_ats",
        ),
        sa.CheckConstraint(
            _sha256_check("catalog_key"),
            name="ck_employer_catalog_entries_key",
        ),
        sa.UniqueConstraint(
            "ats",
            "tenant_key",
            "region",
            name="uq_employer_catalog_tenant",
        ),
    )
    op.create_index(
        "ix_employer_catalog_entries_enabled_ats",
        "employer_catalog_entries",
        ["enabled", "ats"],
    )

    op.create_table(
        "discovery_cursors",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("cursor_key", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "source_key",
            sa.String(255),
            sa.ForeignKey("discovery_sources.source_key", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "catalog_entry_id",
            sa.Integer(),
            sa.ForeignKey("employer_catalog_entries.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("cursor_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("etag", sa.String(255), nullable=True),
        sa.Column("last_modified", sa.String(255), nullable=True),
        sa.Column("last_seen_posting_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(_sha256_check("cursor_key"), name="ck_discovery_cursors_key"),
    )
    op.create_index(
        "ix_discovery_cursors_source_catalog",
        "discovery_cursors",
        ["source_key", "catalog_entry_id"],
    )

    op.create_table(
        "job_source_occurrences",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("occurrence_key", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "job_id",
            sa.Integer(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_key", sa.String(255), nullable=False),
        sa.Column(
            "catalog_entry_id",
            sa.Integer(),
            sa.ForeignKey("employer_catalog_entries.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("external_posting_id", sa.String(255), nullable=True),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("normalized_url_hash", sa.String(64), nullable=False),
        sa.Column("revision_digest", sa.String(64), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.CheckConstraint(
            _sha256_check("occurrence_key"),
            name="ck_job_source_occurrences_key",
        ),
        sa.CheckConstraint(
            _sha256_check("normalized_url_hash"),
            name="ck_job_source_occurrences_url_hash",
        ),
        sa.CheckConstraint(
            _sha256_check("revision_digest"),
            name="ck_job_source_occurrences_revision",
        ),
    )
    op.create_index(
        "ix_job_source_occurrences_job_active",
        "job_source_occurrences",
        ["job_id", "active"],
    )
    op.create_index(
        "ix_job_source_occurrences_source_external",
        "job_source_occurrences",
        ["source_key", "external_posting_id"],
    )
    op.create_index(
        "ix_job_source_occurrences_catalog_external",
        "job_source_occurrences",
        ["catalog_entry_id", "external_posting_id"],
    )
    op.create_index(
        "ix_job_source_occurrences_last_seen",
        "job_source_occurrences",
        ["source_key", "last_seen_at"],
    )

    connection = op.get_bind()
    jobs = connection.execute(
        sa.text(
            "SELECT id, title, company, location, description, requirements, "
            "apply_url, source_url, apply_url_hash, discovery_source, created_at FROM jobs"
        )
    ).mappings()
    occurrence_table = sa.table(
        "job_source_occurrences",
        sa.column("occurrence_key", sa.String),
        sa.column("job_id", sa.Integer),
        sa.column("source_key", sa.String),
        sa.column("catalog_entry_id", sa.Integer),
        sa.column("external_posting_id", sa.String),
        sa.column("normalized_url", sa.Text),
        sa.column("normalized_url_hash", sa.String),
        sa.column("revision_digest", sa.String),
        sa.column("first_seen_at", sa.DateTime),
        sa.column("last_seen_at", sa.DateTime),
        sa.column("closed_at", sa.DateTime),
        sa.column("active", sa.Boolean),
    )
    for row in jobs:
        source_key = str(row["discovery_source"] or "legacy_manual")
        normalized_url = str(row["apply_url"] or row["source_url"] or "")
        normalized_url_hash = (
            str(row["apply_url_hash"])
            if _valid_digest(row["apply_url_hash"])
            else _digest(normalized_url)
        )
        revision_payload = {
            "title": row["title"] or "",
            "company": row["company"] or "",
            "location": row["location"] or "",
            "description": row["description"] or "",
            "requirements": row["requirements"] or "",
            "url": normalized_url,
        }
        revision_digest = _digest(json.dumps(revision_payload, ensure_ascii=False, sort_keys=True))
        occurrence_key = _digest(f"{source_key}|legacy-job:{row['id']}|{normalized_url_hash}")
        first_seen_at = _datetime(row["created_at"])
        connection.execute(
            occurrence_table.insert().values(
                occurrence_key=occurrence_key,
                job_id=row["id"],
                source_key=source_key,
                catalog_entry_id=None,
                external_posting_id=None,
                normalized_url=normalized_url,
                normalized_url_hash=normalized_url_hash,
                revision_digest=revision_digest,
                first_seen_at=first_seen_at,
                last_seen_at=first_seen_at,
                closed_at=None,
                active=True,
            )
        )


def downgrade() -> None:
    op.drop_index("ix_job_source_occurrences_last_seen", table_name="job_source_occurrences")
    op.drop_index(
        "ix_job_source_occurrences_catalog_external",
        table_name="job_source_occurrences",
    )
    op.drop_index(
        "ix_job_source_occurrences_source_external",
        table_name="job_source_occurrences",
    )
    op.drop_index("ix_job_source_occurrences_job_active", table_name="job_source_occurrences")
    op.drop_table("job_source_occurrences")
    op.drop_index(
        "ix_discovery_cursors_source_catalog",
        table_name="discovery_cursors",
    )
    op.drop_table("discovery_cursors")
    op.drop_index(
        "ix_employer_catalog_entries_enabled_ats",
        table_name="employer_catalog_entries",
    )
    op.drop_table("employer_catalog_entries")
    op.drop_index("ix_discovery_sources_due", table_name="discovery_sources")
    op.drop_table("discovery_sources")
    op.drop_index(
        "ix_search_intent_revisions_active_version",
        table_name="search_intent_revisions",
    )
    op.drop_index(
        "uq_search_intent_revisions_one_active",
        table_name="search_intent_revisions",
    )
    op.drop_table("search_intent_revisions")
    with op.batch_alter_table("discovery_runs") as batch_op:
        batch_op.drop_column("closed")
        batch_op.drop_column("duplicates")
        batch_op.drop_column("updated")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("terminal_skip_at")
