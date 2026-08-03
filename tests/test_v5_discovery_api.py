from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from api.routes import control as control_routes
from api.routes import discovery as discovery_routes
from core.config import Settings
from db.models import Base, DiscoveryRun, DiscoverySourceState
from db.session import get_db
from discovery.contracts import SearchIntentV1, stable_digest
from discovery.search_intents import search_intent_payload


@contextmanager
def _client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'discovery-api.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(control_routes.router)
    app.include_router(discovery_routes.router)
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _source() -> DiscoverySourceState:
    return DiscoverySourceState(
        source_key="remotive",
        source_type="remotive",
        descriptor_version="1.0.0",
        transport="public_api",
        authentication_mode="none",
        host="remotive.com",
        cadence_seconds=21_600,
        enabled=True,
        health_status="healthy",
    )


def _intent() -> SearchIntentV1:
    payload = {
        "cv_id": "ml-engineer",
        "titles": ("Machine Learning Engineer",),
        "skills": ("Python", "PyTorch"),
        "seniority": ("mid",),
        "locations": ("Israel", "Worldwide Remote"),
    }
    return SearchIntentV1(intent_id=stable_digest(payload), **payload)


def test_discovery_source_and_run_endpoints_include_dedup_counters(tmp_path):
    with _client(tmp_path) as (client, factory):
        db = factory()
        db.add(_source())
        db.add(
            DiscoveryRun(
                source="remotive",
                status="success",
                inserted=3,
                updated=2,
                duplicates=5,
                closed=1,
                started_at=datetime.now(UTC).replace(tzinfo=None),
                finished_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        db.commit()
        db.close()

        sources = client.get("/api/discovery/sources")
        runs = client.get("/api/discovery/runs")

    assert sources.status_code == 200
    assert sources.json()[0]["source_key"] == "remotive"
    assert runs.status_code == 200
    assert {
        key: runs.json()[0][key] for key in ("inserted", "updated", "duplicates", "closed")
    } == {"inserted": 3, "updated": 2, "duplicates": 5, "closed": 1}


def test_compatibility_overview_uses_registered_source_type_and_health(tmp_path):
    with _client(tmp_path) as (client, factory):
        db = factory()
        source = _source()
        source.health_status = "degraded"
        source.last_error_code = "SOURCE_TIMEOUT"
        db.add(source)
        db.add(
            DiscoveryRun(
                source="remotive",
                status="failed",
                inserted=0,
                reason_code="SOURCE_TIMEOUT",
                started_at=datetime.now(UTC).replace(tzinfo=None),
                finished_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        db.commit()
        db.close()

        response = client.get("/api/control/overview")

    assert response.status_code == 200
    discovery = response.json()["discovery"]
    assert len(discovery) == 1
    assert {
        key: discovery[0][key]
        for key in ("source", "source_type", "enabled", "status", "reason_code")
    } == {
        "source": "remotive",
        "source_type": "remotive",
        "enabled": True,
        "status": "degraded",
        "reason_code": "SOURCE_TIMEOUT",
    }


def test_discovery_run_rejects_unknown_source_and_queues_known_source(tmp_path):
    settings = Settings(_env_file=None, tasks_always_eager=False)
    task = MagicMock()
    with _client(tmp_path) as (client, factory):
        db = factory()
        db.add(_source())
        db.commit()
        db.close()

        with (
            patch("core.config.get_settings", return_value=settings),
            patch.object(discovery_routes, "discover_jobs_task", task),
            patch.object(discovery_routes, "publish_configured_task") as publish,
        ):
            unknown = client.post(
                "/api/discovery/run",
                json={"source_key": "not-configured", "force": True},
            )
            accepted = client.post(
                "/api/discovery/run",
                json={"source_key": "remotive", "force": True},
            )

    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "DISCOVERY_SOURCE_NOT_FOUND"
    assert accepted.status_code == 202
    assert accepted.json() == {
        "accepted": True,
        "state": "queued",
        "source_key": "remotive",
        "force": True,
    }
    publish.assert_called_once_with(task, force=True, source_key="remotive")


def test_discovery_run_fails_closed_when_broker_is_unavailable(tmp_path):
    settings = Settings(_env_file=None, tasks_always_eager=False)
    task = MagicMock()
    with _client(tmp_path) as (client, factory):
        db = factory()
        db.add(_source())
        db.commit()
        db.close()
        with (
            patch("core.config.get_settings", return_value=settings),
            patch.object(discovery_routes, "discover_jobs_task", task),
            patch.object(
                discovery_routes,
                "publish_configured_task",
                side_effect=RuntimeError("fixture broker unavailable"),
            ),
        ):
            response = client.post(
                "/api/discovery/run",
                json={"source_key": "remotive", "force": True},
            )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "DISCOVERY_QUEUE_UNAVAILABLE"


def test_search_intent_preview_and_activation_are_digest_bound(tmp_path):
    intent = _intent()
    payload_json, digest = search_intent_payload((intent,))
    derived = ((intent,), payload_json, digest)
    with _client(tmp_path) as (client, _factory):
        with patch.object(discovery_routes, "_derive_current_intents", return_value=derived):
            first_preview = client.post("/api/search-intent/preview")
            stale = client.post(
                "/api/search-intent/activate",
                json={"expected_digest": "0" * 64},
            )
            activated = client.post(
                "/api/search-intent/activate",
                json={"expected_digest": digest},
            )
            second_preview = client.post("/api/search-intent/preview")

    assert first_preview.status_code == 200
    assert first_preview.json()["activated"] is False
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "SEARCH_INTENT_CHANGED"
    assert activated.status_code == 200
    assert activated.json()["version"] == 1
    assert second_preview.status_code == 200
    assert second_preview.json()["activated"] is True
    assert second_preview.json()["active_version"] == 1
    assert second_preview.json()["intents"] == first_preview.json()["intents"]
