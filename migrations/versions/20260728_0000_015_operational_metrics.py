"""Add durable bounded operational metric events and rollups.

Revision ID: 015_operational_metrics
Revises: 014_control_grant_revocations
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015_operational_metrics"
down_revision: str | None = "014_control_grant_revocations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METRIC_NAMES = (
    "metric_name IN ('attempt_stage', 'attempt_outcome', 'retry', "
    "'governor_denial', 'discovery_result', 'form_resolution', "
    "'attachment_result', 'browser_failure', 'outbound_result')"
)
_LABEL_LENGTHS = (
    "length(ats) BETWEEN 1 AND 32 "
    "AND length(adapter_version) BETWEEN 1 AND 32 "
    "AND length(selector_version) BETWEEN 1 AND 64 "
    "AND length(stage) BETWEEN 1 AND 24 "
    "AND length(outcome) BETWEEN 1 AND 32 "
    "AND length(reason_code) BETWEEN 1 AND 64 "
    "AND length(field_type) BETWEEN 1 AND 24 "
    "AND length(resolver) BETWEEN 1 AND 40 "
    "AND length(attachment_result) BETWEEN 1 AND 24 "
    "AND length(evidence_type) BETWEEN 1 AND 48"
)
_ROLLUP_TOTALS = (
    "event_count >= 0 AND duration_count >= 0 AND duration_sum_ms >= 0 "
    "AND duration_le_1s >= 0 AND duration_le_5s >= duration_le_1s "
    "AND duration_le_15s >= duration_le_5s "
    "AND duration_le_60s >= duration_le_15s "
    "AND duration_le_300s >= duration_le_60s "
    "AND duration_le_900s >= duration_le_300s "
    "AND duration_le_inf >= duration_le_900s "
    "AND duration_le_inf = duration_count"
)


def _sha256_check(column_name: str) -> str:
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


def _label_columns() -> list[sa.Column]:
    return [
        sa.Column("metric_name", sa.String(length=32), nullable=False),
        sa.Column("ats", sa.String(length=32), nullable=False, server_default="none"),
        sa.Column(
            "adapter_version",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
        sa.Column(
            "selector_version",
            sa.String(length=64),
            nullable=False,
            server_default="none",
        ),
        sa.Column("stage", sa.String(length=24), nullable=False, server_default="none"),
        sa.Column("outcome", sa.String(length=32), nullable=False, server_default="none"),
        sa.Column(
            "reason_code",
            sa.String(length=64),
            nullable=False,
            server_default="NONE",
        ),
        sa.Column(
            "field_type",
            sa.String(length=24),
            nullable=False,
            server_default="none",
        ),
        sa.Column("resolver", sa.String(length=40), nullable=False, server_default="none"),
        sa.Column(
            "attachment_result",
            sa.String(length=24),
            nullable=False,
            server_default="none",
        ),
        sa.Column(
            "evidence_type",
            sa.String(length=48),
            nullable=False,
            server_default="none",
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "operational_metric_receipts",
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            _sha256_check("event_key"),
            name="ck_operational_metric_receipts_event_key",
        ),
        sa.PrimaryKeyConstraint("event_key"),
    )

    op.create_table(
        "operational_metric_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("entity_key", sa.String(length=64), nullable=False),
        *_label_columns(),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            f"{_sha256_check('event_key')} AND {_sha256_check('entity_key')}",
            name="ck_operational_metric_events_digests",
        ),
        sa.CheckConstraint(_METRIC_NAMES, name="ck_operational_metric_events_name"),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms BETWEEN 0 AND 604800000",
            name="ck_operational_metric_events_duration",
        ),
        sa.CheckConstraint(
            _LABEL_LENGTHS,
            name="ck_operational_metric_events_label_lengths",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["event_key"],
            ["operational_metric_receipts.event_key"],
            name="fk_operational_metric_events_receipt",
        ),
        sa.UniqueConstraint(
            "event_key",
            name="uq_operational_metric_events_event_key",
        ),
    )
    op.create_index(
        "ix_operational_metric_events_metric_time",
        "operational_metric_events",
        ["metric_name", "occurred_at"],
    )
    op.create_index(
        "ix_operational_metric_events_entity_time",
        "operational_metric_events",
        ["entity_key", "occurred_at"],
    )
    op.create_index(
        "ix_operational_metric_events_occurred_at",
        "operational_metric_events",
        ["occurred_at"],
    )
    op.create_index(
        "ix_operational_metric_events_failure_cluster",
        "operational_metric_events",
        ["ats", "adapter_version", "selector_version", "reason_code", "occurred_at"],
    )

    op.create_table(
        "operational_metric_rollups",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        *_label_columns(),
        sa.Column("event_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "duration_count",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "duration_sum_ms",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("duration_le_1s", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("duration_le_5s", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "duration_le_15s",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "duration_le_60s",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "duration_le_300s",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "duration_le_900s",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "duration_le_inf",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(_METRIC_NAMES, name="ck_operational_metric_rollups_name"),
        sa.CheckConstraint(
            _LABEL_LENGTHS,
            name="ck_operational_metric_rollups_label_lengths",
        ),
        sa.CheckConstraint(_ROLLUP_TOTALS, name="ck_operational_metric_rollups_totals"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "metric_name",
            "ats",
            "adapter_version",
            "selector_version",
            "stage",
            "outcome",
            "reason_code",
            "field_type",
            "resolver",
            "attachment_result",
            "evidence_type",
            name="uq_operational_metric_rollups_dimensions",
        ),
    )
    op.create_index(
        "ix_operational_metric_rollups_metric",
        "operational_metric_rollups",
        ["metric_name"],
    )
    op.create_index(
        "ix_operational_metric_rollups_failure_cluster",
        "operational_metric_rollups",
        ["ats", "adapter_version", "selector_version", "reason_code"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_operational_metric_rollups_failure_cluster",
        table_name="operational_metric_rollups",
    )
    op.drop_index(
        "ix_operational_metric_rollups_metric",
        table_name="operational_metric_rollups",
    )
    op.drop_table("operational_metric_rollups")
    op.drop_index(
        "ix_operational_metric_events_failure_cluster",
        table_name="operational_metric_events",
    )
    op.drop_index(
        "ix_operational_metric_events_occurred_at",
        table_name="operational_metric_events",
    )
    op.drop_index(
        "ix_operational_metric_events_entity_time",
        table_name="operational_metric_events",
    )
    op.drop_index(
        "ix_operational_metric_events_metric_time",
        table_name="operational_metric_events",
    )
    op.drop_table("operational_metric_events")
    op.drop_table("operational_metric_receipts")
