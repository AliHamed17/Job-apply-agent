"""Durability, cardinality, and privacy tests for operational metrics."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY, CollectorRegistry, generate_latest
from sqlalchemy import BigInteger, create_engine, func
from sqlalchemy.orm import sessionmaker

import core.operational_metrics as operational_metrics
from core.operational_labels import QUEUE_LABELS
from core.operational_metrics import (
    DurableOperationalCollector,
    authoritative_queue_depths,
    record_attempt_stage,
    record_operational_event,
    register_durable_operational_collector,
)
from db.models import (
    Application,
    Base,
    Job,
    JobStatus,
    OperationalMetricEvent,
    OperationalMetricReceipt,
    OperationalMetricRollup,
    Submission,
    SubmissionStatus,
)


def _sqlite_factory(path):
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine)


def _record_stage(
    db,
    dedup_key: str,
    *,
    occurred_at: datetime | None = None,
    stage: str = "queued",
) -> bool:
    return record_operational_event(
        db,
        dedup_key=dedup_key,
        entity_key=f"attempt:{dedup_key}",
        metric_name="attempt_stage",
        ats="greenhouse",
        adapter_version="1.0.0",
        selector_version="greenhouse-candidate-v9",
        stage=stage,
        occurred_at=occurred_at,
        duration_seconds=2.5,
    )


def test_attempt_stage_duration_is_labeled_with_exited_stage_and_records_terminal_stage(
    tmp_path,
):
    engine, factory = _sqlite_factory(tmp_path / "metric-stage-duration.db")
    started = datetime.now(UTC).replace(tzinfo=None)
    attempt = SimpleNamespace(
        id=73,
        adapter_name=None,
        submitter_name="unknown",
        adapter_version=None,
        selector_version=None,
    )
    db = factory()
    try:
        assert record_attempt_stage(
            db,
            attempt,
            stage="queued",
            occurred_at=started,
            transition_key="initial",
        )
        assert record_attempt_stage(
            db,
            attempt,
            stage="inspecting",
            previous_stage="queued",
            occurred_at=started + timedelta(seconds=2),
            transition_key="queued-to-inspecting",
        )
        assert record_attempt_stage(
            db,
            attempt,
            stage="preparing",
            previous_stage="inspecting",
            occurred_at=started + timedelta(seconds=5),
            transition_key="inspecting-to-preparing",
        )
        assert record_attempt_stage(
            db,
            attempt,
            stage="finished",
            previous_stage="preparing",
            occurred_at=started + timedelta(seconds=9),
            transition_key="finished:failed_before_commit",
        )
        assert not record_attempt_stage(
            db,
            attempt,
            stage="finished",
            previous_stage="preparing",
            occurred_at=started + timedelta(seconds=9),
            transition_key="finished:failed_before_commit",
        )
        db.commit()

        events = (
            db.query(OperationalMetricEvent)
            .filter(OperationalMetricEvent.metric_name == "attempt_stage")
            .order_by(OperationalMetricEvent.occurred_at, OperationalMetricEvent.id)
            .all()
        )
        assert [(event.stage, event.duration_ms) for event in events] == [
            ("queued", None),
            ("queued", 2_000),
            ("inspecting", 3_000),
            ("preparing", 4_000),
        ]

        timed_rollups = (
            db.query(OperationalMetricRollup)
            .filter(
                OperationalMetricRollup.metric_name == "attempt_stage",
                OperationalMetricRollup.duration_count > 0,
            )
            .all()
        )
        assert {row.stage: (row.duration_count, row.duration_sum_ms) for row in timed_rollups} == {
            "queued": (1, 2_000),
            "inspecting": (1, 3_000),
            "preparing": (1, 4_000),
        }
    finally:
        db.close()
        engine.dispose()


def test_sqlite_concurrent_redelivery_increments_rollup_once(tmp_path):
    engine, factory = _sqlite_factory(tmp_path / "metric-dedup.db")
    barrier = Barrier(2)

    def record() -> bool:
        db = factory()
        try:
            barrier.wait()
            inserted = _record_stage(db, "same-domain-transition")
            db.commit()
            return inserted
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            inserted = list(pool.map(lambda _item: record(), range(2)))
        assert sum(inserted) == 1

        db = factory()
        assert db.query(OperationalMetricEvent).count() == 1
        assert db.query(OperationalMetricReceipt).count() == 1
        rollup = db.query(OperationalMetricRollup).one()
        assert rollup.event_count == 1
        assert rollup.duration_count == 1
        assert rollup.duration_le_5s == 1
        db.close()
    finally:
        engine.dispose()


def test_event_writes_defer_one_prune_until_outer_batch_commit(tmp_path, monkeypatch):
    engine, factory = _sqlite_factory(tmp_path / "metric-batch-maintenance.db")
    prune_calls = []

    def observe_prune(db, *, now=None):
        prune_calls.append((db, now))
        return 0

    monkeypatch.setattr(operational_metrics, "prune_operational_events", observe_prune)
    db = factory()
    try:
        for index in range(200):
            assert _record_stage(db, f"bounded-form-plan-event-{index}")

        assert prune_calls == []
        with db.begin_nested():
            assert _record_stage(db, "bounded-form-plan-event-200")
        assert prune_calls == []
        assert db.query(OperationalMetricEvent).count() == 201
        assert db.query(OperationalMetricRollup).one().event_count == 201

        db.commit()
        assert prune_calls == [(db, None)]
        assert db.query(OperationalMetricEvent).count() == 201
        assert db.query(OperationalMetricReceipt).count() == 201
        assert db.query(OperationalMetricRollup).one().event_count == 201
    finally:
        db.close()
        engine.dispose()


def test_rollback_and_dedup_only_commit_do_not_schedule_pruning(tmp_path, monkeypatch):
    engine, factory = _sqlite_factory(tmp_path / "metric-maintenance-rollback.db")
    prune_calls = []

    def observe_prune(db, *, now=None):
        prune_calls.append((db, now))
        return 0

    monkeypatch.setattr(operational_metrics, "prune_operational_events", observe_prune)
    db = factory()
    try:
        assert _record_stage(db, "rolled-back-operational-event")
        db.rollback()
        db.commit()
        assert prune_calls == []
        assert db.query(OperationalMetricEvent).count() == 0
        assert db.query(OperationalMetricReceipt).count() == 0
        assert db.query(OperationalMetricRollup).count() == 0

        assert _record_stage(db, "committed-operational-event")
        db.commit()
        assert prune_calls == [(db, None)]

        prune_calls.clear()
        assert not _record_stage(db, "committed-operational-event")
        db.commit()
        assert prune_calls == []
        assert db.query(OperationalMetricEvent).count() == 1
        assert db.query(OperationalMetricReceipt).count() == 1
        assert db.query(OperationalMetricRollup).one().event_count == 1
    finally:
        db.close()
        engine.dispose()


def test_nested_rollback_discards_only_its_maintenance_request(tmp_path, monkeypatch):
    engine, factory = _sqlite_factory(tmp_path / "metric-nested-rollback.db")
    prune_calls = []

    def observe_prune(db, *, now=None):
        prune_calls.append((db, now))
        return 0

    monkeypatch.setattr(operational_metrics, "prune_operational_events", observe_prune)
    db = factory()
    try:
        assert _record_stage(db, "outer-operational-event")
        with pytest.raises(RuntimeError, match="rollback nested metric"):
            with db.begin_nested():
                assert _record_stage(db, "nested-rolled-back-operational-event")
                raise RuntimeError("rollback nested metric")

        assert prune_calls == []
        db.commit()
        assert prune_calls == [(db, None)]
        assert db.query(OperationalMetricEvent).count() == 1
        assert db.query(OperationalMetricReceipt).count() == 1
        assert db.query(OperationalMetricRollup).one().event_count == 1
    finally:
        db.close()
        engine.dispose()


def test_event_detail_retention_enforces_window_and_exact_cap(tmp_path, monkeypatch):
    engine, factory = _sqlite_factory(tmp_path / "metric-retention.db")
    monkeypatch.setattr(operational_metrics, "OPERATIONAL_EVENT_MAX_ROWS", 3)
    now = datetime.now(UTC).replace(tzinfo=None)
    db = factory()
    try:
        for index in range(5):
            assert _record_stage(
                db,
                f"retained-{index}",
                occurred_at=now + timedelta(seconds=index),
            )
        assert _record_stage(
            db,
            "expired-detail",
            occurred_at=now
            - timedelta(days=operational_metrics.OPERATIONAL_EVENT_RETENTION_DAYS + 1),
        )
        db.commit()

        retained = (
            db.query(OperationalMetricEvent)
            .order_by(OperationalMetricEvent.occurred_at, OperationalMetricEvent.id)
            .all()
        )
        assert len(retained) == 3
        assert db.query(OperationalMetricReceipt).count() == 6
        assert all(
            row.occurred_at
            >= now - timedelta(days=operational_metrics.OPERATIONAL_EVENT_RETENTION_DAYS)
            for row in retained
        )
        assert sum(row.event_count for row in db.query(OperationalMetricRollup).all()) == 6
    finally:
        db.close()
        engine.dispose()


def test_concurrent_writers_leave_exact_detail_cap_and_all_receipts(tmp_path, monkeypatch):
    engine, factory = _sqlite_factory(tmp_path / "metric-concurrent-cap.db")
    monkeypatch.setattr(operational_metrics, "OPERATIONAL_EVENT_MAX_ROWS", 1)
    barrier = Barrier(2)

    def record(index: int) -> None:
        db = factory()
        try:
            barrier.wait()
            assert _record_stage(db, f"concurrent-cap-{index}")
            db.commit()
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(record, range(2)))
        db = factory()
        assert db.query(OperationalMetricEvent).count() == 1
        assert db.query(OperationalMetricReceipt).count() == 2
        assert db.query(OperationalMetricRollup).one().event_count == 2
        db.close()
    finally:
        engine.dispose()


def test_expired_detail_replay_is_rejected_by_permanent_receipt(tmp_path):
    engine, factory = _sqlite_factory(tmp_path / "metric-expired-replay.db")
    now = datetime.now(UTC).replace(tzinfo=None)
    expired_at = now - timedelta(days=operational_metrics.OPERATIONAL_EVENT_RETENTION_DAYS + 1)
    db = factory()
    try:
        assert _record_stage(db, "expired-replay", occurred_at=expired_at)
        db.commit()
        assert db.query(OperationalMetricEvent).count() == 0
        assert db.query(OperationalMetricReceipt).count() == 1
        assert db.query(OperationalMetricRollup).one().event_count == 1

        assert not _record_stage(db, "expired-replay", occurred_at=now)
        db.commit()
        assert db.query(OperationalMetricEvent).count() == 0
        assert db.query(OperationalMetricReceipt).count() == 1
        assert db.query(OperationalMetricRollup).one().event_count == 1
    finally:
        db.close()
        engine.dispose()


def test_collector_renormalizes_hostile_database_labels_and_collapses_series(
    tmp_path,
    monkeypatch,
):
    engine, factory = _sqlite_factory(tmp_path / "metric-privacy.db")
    db = factory()
    try:
        common = {
            "metric_name": "attempt_outcome",
            "stage": "finished",
            "outcome": "failed_before_commit",
            "field_type": "none",
            "attachment_result": "unverified",
            "evidence_type": "none",
            "event_count": 1,
            "duration_count": 1,
            "duration_sum_ms": 2_000,
            "duration_le_1s": 0,
            "duration_le_5s": 1,
            "duration_le_15s": 1,
            "duration_le_60s": 1,
            "duration_le_300s": 1,
            "duration_le_900s": 1,
            "duration_le_inf": 1,
        }
        rows = [
            OperationalMetricRollup(
                **common,
                ats="candidate@example.com",
                adapter_version="https://private.example/cv",
                selector_version="+972501234567",
                reason_code="candidate@example.com",
                resolver="secret answer",
            ),
            OperationalMetricRollup(
                **common,
                ats="greenhouse",
                adapter_version="0.9.0",
                selector_version="retired-private-selector",
                reason_code="PRIVATE FAILURE TEXT",
                resolver="old resolver",
            ),
        ]
        for index in range(100):
            rows.append(
                OperationalMetricRollup(
                    **common,
                    ats=f"person{index}@x.test",
                    adapter_version=f"private-{index}",
                    selector_version=f"selector-{index}",
                    reason_code=f"private-reason-{index}",
                    resolver=f"private-{index}",
                )
            )
        db.add_all(rows)
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr("db.session.get_session_factory", lambda: factory)
    registry = CollectorRegistry()
    registry.register(DurableOperationalCollector())
    exposition = generate_latest(registry).decode()

    try:
        assert 'ats="other"' in exposition
        assert 'adapter_version="other"' in exposition
        assert 'selector_version="other"' in exposition
        assert 'reason_code="OTHER"' in exposition
        assert exposition.count("job_agent_operational_events_total{") == 2
        assert (
            'job_agent_operational_events_total{adapter_version="other",ats="other"' in exposition
        )
        assert "} 101.0" in exposition
        for forbidden in (
            "candidate@example.com",
            "private.example",
            "+972501234567",
            "retired-private-selector",
            "PRIVATE FAILURE TEXT",
            "secret answer",
        ):
            assert forbidden not in exposition
    finally:
        engine.dispose()


def test_collector_registration_survives_database_outage_reload_and_recovery(
    tmp_path,
    monkeypatch,
):
    engine, factory = _sqlite_factory(tmp_path / "collector-recovery.db")
    db = factory()
    assert _record_stage(db, "collector-recovery")
    db.commit()
    db.close()

    def database_down():
        raise ConnectionError("database unavailable")

    monkeypatch.setattr("db.session.get_session_factory", database_down)
    registry = CollectorRegistry(auto_describe=True)
    first = register_durable_operational_collector(registry)
    assert first is not None
    down_exposition = generate_latest(registry).decode()
    assert down_exposition.count("# HELP job_agent_operational_events_total") == 1
    assert down_exposition.count("# HELP job_agent_operational_duration_seconds") == 1

    delattr(registry, operational_metrics._REGISTRY_COLLECTOR_ATTR)
    reloaded = register_durable_operational_collector(registry)
    assert reloaded is first

    monkeypatch.setattr("db.session.get_session_factory", lambda: factory)
    recovered = generate_latest(registry).decode()
    try:
        assert recovered.count("# HELP job_agent_operational_events_total") == 1
        assert recovered.count("job_agent_operational_events_total{") == 1
        assert "} 1.0" in recovered
    finally:
        engine.dispose()


def test_queue_gauges_are_refreshed_from_shared_database_state(tmp_path, monkeypatch):
    from core.metrics import refresh_authoritative_metrics

    engine, factory = _sqlite_factory(tmp_path / "queue-gauges.db")
    db = factory()
    db.add(
        Job(
            title="Private queue title",
            source_url="https://private.example/job",
            status=JobStatus.EXTRACTED,
        )
    )
    for index, outcome in enumerate(("unknown", "operator_confirmed"), start=1):
        job = Job(
            title=f"Private queue application {index}",
            source_url=f"https://private.example/application/{index}",
            status=JobStatus.NEEDS_REVIEW,
        )
        application = Application(job=job, status=JobStatus.NEEDS_REVIEW)
        db.add(application)
        db.flush()
        db.add(
            Submission(
                application_id=application.id,
                attempt_number=1,
                idempotency_key=f"queue-outcome-{index}",
                submitter_name="greenhouse",
                status=SubmissionStatus.UNKNOWN,
                stage="finished",
                outcome=outcome,
                finished_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
    db.commit()
    assert authoritative_queue_depths(db)["submissions_unknown"] == 1
    db.close()
    monkeypatch.setattr("db.session.get_session_factory", lambda: factory)

    try:
        refresh_authoritative_metrics()
        exposition = generate_latest(REGISTRY).decode()
        assert 'job_agent_queue_depth{queue="jobs_extracted"} 1.0' in exposition
        assert "job_agent_queue_snapshot_available 1.0" in exposition
        for queue in QUEUE_LABELS:
            assert exposition.count(f'job_agent_queue_depth{{queue="{queue}"}}') == 1
        assert "Private queue title" not in exposition
        assert "private.example" not in exposition
    finally:
        engine.dispose()


def test_rollup_cumulative_columns_are_64_bit():
    cumulative_columns = (
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
    )
    assert all(
        isinstance(OperationalMetricRollup.__table__.c[name].type, BigInteger)
        for name in cumulative_columns
    )


def test_postgres_pruning_uses_transaction_advisory_lock():
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    db = MagicMock()
    db.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    operational_metrics._coordinate_operational_pruning(db)

    statement = str(db.execute.call_args.args[0])
    assert "pg_advisory_xact_lock" in statement


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)
def test_postgres_concurrent_redelivery_increments_rollup_once():
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(bind=engine)
    barrier = Barrier(2)
    token = f"postgres-operational-{uuid4().hex}"

    def record() -> bool:
        db = factory()
        try:
            barrier.wait()
            inserted = _record_stage(db, token)
            db.commit()
            return inserted
        finally:
            db.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            inserted = list(pool.map(lambda _item: record(), range(2)))
        assert sum(inserted) == 1
        db = factory()
        digest = operational_metrics._digest("event", token)
        event = (
            db.query(OperationalMetricEvent)
            .filter(OperationalMetricEvent.event_key == digest)
            .one()
        )
        assert event.metric_name == "attempt_stage"
        receipt = db.get(OperationalMetricReceipt, digest)
        assert receipt is not None
        rollup = (
            db.query(OperationalMetricRollup)
            .filter(
                OperationalMetricRollup.metric_name == event.metric_name,
                OperationalMetricRollup.ats == event.ats,
                OperationalMetricRollup.adapter_version == event.adapter_version,
                OperationalMetricRollup.selector_version == event.selector_version,
                OperationalMetricRollup.stage == event.stage,
                OperationalMetricRollup.outcome == event.outcome,
                OperationalMetricRollup.reason_code == event.reason_code,
                OperationalMetricRollup.field_type == event.field_type,
                OperationalMetricRollup.resolver == event.resolver,
                OperationalMetricRollup.attachment_result == event.attachment_result,
                OperationalMetricRollup.evidence_type == event.evidence_type,
            )
            .one()
        )
        db.delete(event)
        db.flush()
        db.delete(receipt)
        if rollup.event_count == 1:
            db.delete(rollup)
        else:
            rollup.event_count -= 1
            rollup.duration_count -= 1
            rollup.duration_sum_ms -= 2_500
            rollup.duration_le_5s -= 1
            rollup.duration_le_15s -= 1
            rollup.duration_le_60s -= 1
            rollup.duration_le_300s -= 1
            rollup.duration_le_900s -= 1
            rollup.duration_le_inf -= 1
        db.commit()
        db.close()
    finally:
        engine.dispose()


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)
def test_postgres_concurrent_batch_commits_leave_exact_detail_cap(monkeypatch):
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(bind=engine)
    seed_time = datetime(1900, 1, 1)
    recent_time = datetime.now(UTC).replace(tzinfo=None)
    token_stages = {
        f"postgres-prune-seed-a-{uuid4().hex}": "queued",
        f"postgres-prune-seed-b-{uuid4().hex}": "inspecting",
        f"postgres-prune-recent-a-{uuid4().hex}": "preparing",
        f"postgres-prune-recent-b-{uuid4().hex}": "ready",
    }
    tokens = list(token_stages)
    seed_tokens = tokens[:2]
    recent_tokens = tokens[2:]

    db = factory()
    baseline = db.query(func.count(OperationalMetricEvent.id)).scalar() or 0
    db.close()
    monkeypatch.setattr(operational_metrics, "OPERATIONAL_EVENT_MAX_ROWS", baseline + 3)
    monkeypatch.setattr(operational_metrics, "OPERATIONAL_EVENT_RETENTION_DAYS", 100_000)

    try:
        db = factory()
        for offset, token in enumerate(seed_tokens):
            assert _record_stage(
                db,
                token,
                occurred_at=seed_time + timedelta(seconds=offset),
                stage=token_stages[token],
            )
        db.commit()
        db.close()

        barrier = Barrier(2)

        def record(token: str) -> bool:
            writer = factory()
            try:
                barrier.wait()
                inserted = _record_stage(
                    writer,
                    token,
                    occurred_at=recent_time,
                    stage=token_stages[token],
                )
                writer.commit()
                return inserted
            finally:
                writer.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            inserted = list(pool.map(record, recent_tokens))
        assert inserted == [True, True]

        db = factory()
        try:
            assert db.query(OperationalMetricEvent).count() == baseline + 3
            retained_keys = {
                row.event_key
                for row in db.query(OperationalMetricEvent.event_key)
                .filter(
                    OperationalMetricEvent.event_key.in_(
                        [operational_metrics._digest("event", token) for token in tokens]
                    )
                )
                .all()
            }
            assert retained_keys == {
                operational_metrics._digest("event", token)
                for token in (seed_tokens[1], *recent_tokens)
            }
            assert all(
                db.get(
                    OperationalMetricReceipt,
                    operational_metrics._digest("event", token),
                )
                is not None
                for token in tokens
            )
        finally:
            db.close()
    finally:
        cleanup = factory()
        try:
            for token, stage in token_stages.items():
                event_key = operational_metrics._digest("event", token)
                receipt = cleanup.get(OperationalMetricReceipt, event_key)
                if receipt is None:
                    continue
                cleanup.query(OperationalMetricEvent).filter(
                    OperationalMetricEvent.event_key == event_key
                ).delete(synchronize_session=False)
                cleanup.delete(receipt)

                labels = operational_metrics.OperationalLabels.normalize(
                    metric_name="attempt_stage",
                    ats="greenhouse",
                    adapter_version="1.0.0",
                    selector_version="greenhouse-candidate-v9",
                    stage=stage,
                )
                rollup_query = cleanup.query(OperationalMetricRollup)
                for name, value in labels.as_dict().items():
                    rollup_query = rollup_query.filter(
                        getattr(OperationalMetricRollup, name) == value
                    )
                rollup = rollup_query.one()
                rollup.event_count -= 1
                rollup.duration_count -= 1
                rollup.duration_sum_ms -= 2_500
                rollup.duration_le_5s -= 1
                rollup.duration_le_15s -= 1
                rollup.duration_le_60s -= 1
                rollup.duration_le_300s -= 1
                rollup.duration_le_900s -= 1
                rollup.duration_le_inf -= 1
                if rollup.event_count == 0:
                    cleanup.delete(rollup)
            cleanup.commit()
            assert cleanup.query(OperationalMetricEvent).count() == baseline
        finally:
            cleanup.close()
            engine.dispose()


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgresql"),
    reason="PostgreSQL integration test",
)
def test_postgres_rollups_cross_signed_32_bit_boundary():
    engine = create_engine(os.environ["DATABASE_URL"])
    factory = sessionmaker(bind=engine)
    maximum_32_bit = 2_147_483_647
    db = factory()
    try:
        db.add(
            OperationalMetricRollup(
                metric_name="outbound_result",
                ats="generic_portal",
                adapter_version="none",
                selector_version="none",
                stage="finished",
                outcome="draft_only",
                reason_code="POLICY",
                field_type="unknown",
                resolver="abstained",
                attachment_result="unknown",
                evidence_type="operator_confirmed",
                event_count=maximum_32_bit,
                duration_count=maximum_32_bit,
                duration_sum_ms=maximum_32_bit,
                duration_le_1s=maximum_32_bit,
                duration_le_5s=maximum_32_bit,
                duration_le_15s=maximum_32_bit,
                duration_le_60s=maximum_32_bit,
                duration_le_300s=maximum_32_bit,
                duration_le_900s=maximum_32_bit,
                duration_le_inf=maximum_32_bit,
            )
        )
        db.flush()
        assert record_operational_event(
            db,
            dedup_key=f"bigint-boundary-{uuid4().hex}",
            entity_key=f"bigint-boundary-{uuid4().hex}",
            metric_name="outbound_result",
            ats="generic_portal",
            adapter_version="none",
            selector_version="none",
            stage="finished",
            outcome="draft_only",
            reason_code="POLICY",
            field_type="unknown",
            resolver="abstained",
            attachment_result="unknown",
            evidence_type="operator_confirmed",
            duration_seconds=0.5,
        )
        db.flush()
        row = (
            db.query(OperationalMetricRollup)
            .filter(
                OperationalMetricRollup.metric_name == "outbound_result",
                OperationalMetricRollup.ats == "generic_portal",
                OperationalMetricRollup.reason_code == "POLICY",
            )
            .one()
        )
        assert row.event_count == maximum_32_bit + 1
        assert row.duration_count == maximum_32_bit + 1
        assert row.duration_sum_ms == maximum_32_bit + 500
        assert row.duration_le_inf == maximum_32_bit + 1
    finally:
        db.rollback()
        db.close()
        engine.dispose()
