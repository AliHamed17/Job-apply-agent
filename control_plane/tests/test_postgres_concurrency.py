from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, inspect, select

from job_control_plane.config import Settings
from job_control_plane.db import Base, build_session_factory
from job_control_plane.models import ReviewGrant, SubmissionCommand
from job_control_plane.protocol import (
    AdapterCode,
    CommandPollEnvelope,
    CommandPollPayload,
    HeartbeatEnvelope,
    HeartbeatPayload,
    ReviewGrantEnvelope,
    ReviewGrantPayload,
    RunnerStatus,
)
from job_control_plane.services import (
    ControlPlaneError,
    create_command,
    poll_command,
    receive_heartbeat,
    receive_review_grant,
)


@pytest.fixture
def postgres_runtime(settings: Settings):
    database_url = os.environ.get("CONTROL_POSTGRES_TEST_URL", "").strip()
    if not database_url:
        pytest.skip("CONTROL_POSTGRES_TEST_URL is not configured")
    engine = create_engine(database_url, pool_pre_ping=True)
    assert "control_submission_commands" in inspect(engine).get_table_names()
    runtime = replace(settings, database_url=database_url)
    factory = build_session_factory(engine)
    with engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    try:
        yield runtime, factory
    finally:
        with engine.begin() as connection:
            for table in reversed(Base.metadata.sorted_tables):
                connection.execute(table.delete())
        engine.dispose()


def _ready_runner(
    runtime: Settings,
    factory,
    sign_runner,
) -> str:
    boot_id = str(uuid4())
    heartbeat = sign_runner(
        HeartbeatEnvelope,
        HeartbeatPayload(
            boot_id=boot_id,
            release_digest="a" * 40,
            status=RunnerStatus.READY,
        ),
    )
    with factory() as db:
        receive_heartbeat(db, runtime, heartbeat)
        db.commit()
    return boot_id


def _review_grant(
    runtime: Settings,
    factory,
    sign_runner,
) -> ReviewGrantEnvelope:
    now = datetime.now(UTC)
    envelope = sign_runner(
        ReviewGrantEnvelope,
        ReviewGrantPayload(
            grant_id=uuid4(),
            application_ref=uuid4(),
            application_revision=1,
            adapter=AdapterCode.WORKDAY,
            adapter_version="2.0.0",
            form_fingerprint_digest="b" * 64,
            reviewed_at=now,
        ),
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    with factory() as db:
        receive_review_grant(db, runtime, envelope, now=now)
        db.commit()
    return envelope


def _create_for_grant(
    runtime: Settings,
    factory,
    grant: ReviewGrantEnvelope,
    *,
    idempotency_key,
    barrier: Barrier,
) -> tuple[str, str]:
    with factory() as db:
        barrier.wait()
        try:
            result = create_command(
                db,
                runtime,
                grant_id=grant.payload.grant_id,
                application_ref=grant.payload.application_ref,
                application_revision=grant.payload.application_revision,
                form_fingerprint_digest=grant.payload.form_fingerprint_digest,
                client_idempotency_key=idempotency_key,
            )
            db.commit()
            return ("duplicate" if result.duplicate else "created", result.command.id)
        except ControlPlaneError as exc:
            db.rollback()
            return ("denied", exc.code)


def test_postgres_concurrent_grant_consumers_create_one_command(
    postgres_runtime,
    sign_runner,
) -> None:
    runtime, factory = postgres_runtime
    _ready_runner(runtime, factory, sign_runner)
    grant = _review_grant(runtime, factory, sign_runner)
    barrier = Barrier(2)
    keys = (uuid4(), uuid4())

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda key: _create_for_grant(
                    runtime,
                    factory,
                    grant,
                    idempotency_key=key,
                    barrier=barrier,
                ),
                keys,
            )
        )

    assert sum(outcome[0] == "created" for outcome in outcomes) == 1
    assert sum(outcome[0] == "denied" for outcome in outcomes) == 1
    with factory() as db:
        count = db.scalar(
            select(func.count())
            .select_from(SubmissionCommand)
            .where(SubmissionCommand.grant_id == str(grant.payload.grant_id))
        )
    assert count == 1


def test_postgres_concurrent_idempotent_requests_create_one_command(
    postgres_runtime,
    sign_runner,
) -> None:
    runtime, factory = postgres_runtime
    _ready_runner(runtime, factory, sign_runner)
    grant = _review_grant(runtime, factory, sign_runner)
    barrier = Barrier(2)
    idempotency_key = uuid4()

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _create_for_grant,
                runtime,
                factory,
                grant,
                idempotency_key=idempotency_key,
                barrier=barrier,
            )
            for _ in range(2)
        ]
        outcomes = [future.result() for future in futures]

    assert sum(outcome[0] == "created" for outcome in outcomes) == 1
    assert sorted(outcome[0] for outcome in outcomes) == ["created", "duplicate"]
    assert outcomes[0][1] == outcomes[1][1]
    with factory() as db:
        count = db.scalar(
            select(func.count())
            .select_from(SubmissionCommand)
            .where(SubmissionCommand.grant_id == str(grant.payload.grant_id))
        )
    assert count == 1


def test_postgres_skip_locked_delivers_one_command_to_one_poller(
    postgres_runtime,
    sign_runner,
) -> None:
    runtime, factory = postgres_runtime
    boot_id = _ready_runner(runtime, factory, sign_runner)
    grant = _review_grant(runtime, factory, sign_runner)
    with factory() as db:
        created = create_command(
            db,
            runtime,
            grant_id=grant.payload.grant_id,
            application_ref=grant.payload.application_ref,
            application_revision=grant.payload.application_revision,
            form_fingerprint_digest=grant.payload.form_fingerprint_digest,
            client_idempotency_key=uuid4(),
        )
        command_id = created.command.id
        db.commit()

    polls = [
        sign_runner(
            CommandPollEnvelope,
            CommandPollPayload(boot_id=boot_id),
        )
        for _ in range(2)
    ]
    barrier = Barrier(2)

    def run_poll(envelope: CommandPollEnvelope) -> list[str]:
        with factory() as db:
            barrier.wait()
            commands = poll_command(db, runtime, envelope)
            identifiers = [str(command.payload.command_id) for command in commands]
            db.commit()
            return identifiers

    with ThreadPoolExecutor(max_workers=2) as executor:
        deliveries = list(executor.map(run_poll, polls))

    assert sorted(map(len, deliveries)) == [0, 1]
    assert command_id in {identifier for delivery in deliveries for identifier in delivery}
    with factory() as db:
        row = db.scalar(select(SubmissionCommand).where(SubmissionCommand.id == command_id))
        assert row is not None
        assert row.status == "claimed"
        assert row.delivery_count == 1
        assert db.scalar(select(func.count()).select_from(ReviewGrant)) == 1
