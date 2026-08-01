"""Authenticated discovery mesh and search-intent control endpoints."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from db.models import DiscoveryRun, DiscoverySourceState, SearchIntentRevision
from db.session import get_db
from worker.discovery_tasks import discover_jobs_task

router = APIRouter(tags=["discovery"])


class DiscoverySourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    source_key: str
    source_type: str
    descriptor_version: str
    configuration_digest: str
    transport: str
    authentication_mode: str
    host: str
    cadence_seconds: int
    enabled: bool
    disabled_reason: str | None
    health_status: str
    next_poll_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None


class DiscoveryRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    status: str
    inserted: int
    updated: int
    duplicates: int
    closed: int
    reason_code: str | None
    started_at: datetime
    finished_at: datetime | None


class DiscoveryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_key: str | None = Field(default=None, min_length=1, max_length=255)
    force: bool = False


class SearchIntentActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def _derive_current_intents(db: Session):
    from profile.cv_routing import load_routing_config

    from core.config import get_settings
    from discovery.search_intents import derive_search_intents, search_intent_payload
    from worker.discovery_tasks import _load_discovery_profile

    settings = get_settings()
    profile, _ = _load_discovery_profile(settings, db)
    routing = load_routing_config(settings.cv_routing_path)
    intents = derive_search_intents(
        routing,
        profile_locations=profile.preferences.locations,
    )
    payload_json, digest = search_intent_payload(intents)
    return intents, payload_json, digest


@router.get("/api/discovery/sources", response_model=list[DiscoverySourceResponse])
def list_discovery_sources(db: Session = Depends(get_db)):
    return db.query(DiscoverySourceState).order_by(DiscoverySourceState.source_key).all()


@router.get("/api/discovery/runs", response_model=list[DiscoveryRunResponse])
def list_discovery_runs(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return (
        db.query(DiscoveryRun)
        .order_by(DiscoveryRun.started_at.desc(), DiscoveryRun.id.desc())
        .limit(limit)
        .all()
    )


@router.post("/api/discovery/run", status_code=status.HTTP_202_ACCEPTED)
def queue_discovery_run(
    payload: DiscoveryRunRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
):
    from core.config import get_settings

    settings = get_settings()
    if payload.source_key is not None:
        exists = (
            db.query(DiscoverySourceState.id)
            .filter(DiscoverySourceState.source_key == payload.source_key)
            .first()
        )
        if exists is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "DISCOVERY_SOURCE_NOT_FOUND"},
            )
    kwargs = {"force": payload.force, "source_key": payload.source_key}
    if settings.tasks_always_eager:
        background.add_task(discover_jobs_task.apply, kwargs=kwargs)
    else:
        try:
            discover_jobs_task.delay(**kwargs)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "DISCOVERY_QUEUE_UNAVAILABLE"},
            ) from exc
    return {
        "accepted": True,
        "state": "queued",
        "source_key": payload.source_key,
        "force": payload.force,
    }


@router.post("/api/search-intent/preview")
def preview_search_intents(db: Session = Depends(get_db)):
    try:
        intents, payload_json, digest = _derive_current_intents(db)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SEARCH_INTENT_CONFIGURATION_INVALID",
                "message": "Review the local CV routing and profile configuration.",
            },
        ) from exc
    active = (
        db.query(SearchIntentRevision)
        .filter(SearchIntentRevision.active.is_(True))
        .order_by(SearchIntentRevision.version.desc())
        .first()
    )
    return {
        "schema_version": "search-intent.v1",
        "digest": digest,
        "count": len(intents),
        "intents": json.loads(payload_json),
        "activated": active is not None and active.payload_digest == digest,
        "active_version": (
            active.version if active is not None and active.payload_digest == digest else None
        ),
    }


@router.post("/api/search-intent/activate")
def activate_search_intent_revision(
    payload: SearchIntentActivateRequest,
    db: Session = Depends(get_db),
):
    from discovery.search_intents import activate_search_intents

    try:
        intents, _payload_json, digest = _derive_current_intents(db)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "SEARCH_INTENT_CONFIGURATION_INVALID"},
        ) from exc
    if digest != payload.expected_digest:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "SEARCH_INTENT_CHANGED",
                "current_digest": digest,
            },
        )
    revision = activate_search_intents(db, intents)
    return {
        "schema_version": revision.schema_version,
        "version": revision.version,
        "digest": revision.payload_digest,
        "count": len(intents),
        "activated_at": revision.activated_at,
    }
