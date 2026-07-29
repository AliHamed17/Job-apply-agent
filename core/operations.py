"""Production operations primitives: rate limits, heartbeats, and readiness."""

from __future__ import annotations

import json
import math
import os
import shutil
import time
from pathlib import Path
from typing import Any

import redis
from sqlalchemy import text
from sqlalchemy.engine import Engine

from core.config import Settings, get_settings
from core.runtime_identity import get_runtime_identity
from db.session import get_engine
from llm.contracts import LLMReasonCode, ModelIdentity
from llm.ollama_runtime import OllamaReadiness, OllamaRuntime

HEARTBEAT_PREFIX = "job-agent:heartbeat:"


def redis_client(settings: Settings | None = None) -> redis.Redis:
    cfg = settings or get_settings()
    return redis.Redis.from_url(cfg.redis_url, decode_responses=True)


def rate_limit_allowed(identity: str, limit: int, settings: Settings | None = None) -> bool:
    """Atomically enforce a fixed one-minute limit in Redis."""
    client = redis_client(settings)
    bucket = int(time.time() // 60)
    key = f"job-agent:rate:{identity}:{bucket}"
    with client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, 120)
        count, _ = pipe.execute()
    return int(count) <= limit


def record_heartbeat(component: str, settings: Settings | None = None) -> None:
    identity = get_runtime_identity()
    payload = json.dumps(
        {
            "seen_at": time.time(),
            "build_sha": identity.build_sha,
            "source_digest": identity.source_digest,
            "release_id": identity.release_id,
            "protocol_version": identity.protocol_version,
        },
        separators=(",", ":"),
    )
    redis_client(settings).set(f"{HEARTBEAT_PREFIX}{component}", payload, ex=3600)


def _heartbeat_status(component: str, settings: Settings) -> dict[str, Any]:
    raw = redis_client(settings).get(f"{HEARTBEAT_PREFIX}{component}")
    if not raw:
        return {"ok": False, "detail": "missing"}
    raw_text = str(raw)
    try:
        payload = json.loads(raw_text)
        seen_at = float(payload["seen_at"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Backward compatibility for workers running the timestamp-only
        # heartbeat format during a rolling upgrade.
        payload = {}
        try:
            seen_at = float(raw_text)
        except ValueError:
            return {"ok": False, "detail": "invalid"}
    if not math.isfinite(seen_at):
        return {"ok": False, "detail": "invalid"}
    age = max(0.0, time.time() - seen_at)
    result: dict[str, Any] = {
        "ok": age <= settings.dependency_heartbeat_ttl_seconds,
        "age_seconds": round(age, 1),
    }
    if isinstance(payload, dict):
        build_sha = payload.get("build_sha")
        source_digest = payload.get("source_digest")
        release_id = payload.get("release_id")
        protocol_version = payload.get("protocol_version")
        if isinstance(build_sha, str):
            result["build_sha"] = build_sha[:64]
        if isinstance(source_digest, str):
            result["source_digest"] = source_digest[:71]
        if isinstance(release_id, str):
            result["release_id"] = release_id[:64]
        if isinstance(protocol_version, str):
            result["protocol_version"] = protocol_version[:64]
    return result


def browser_available() -> bool:
    if shutil.which("chromium") or shutil.which("chromium-browser"):
        return True
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache/ms-playwright"))
    return cache.exists() and any(cache.glob("chromium-*"))


def llm_readiness(settings: Settings | None = None) -> dict[str, Any]:
    """Return bounded local-model readiness without exposing prompts or URLs."""

    cfg = settings or get_settings()
    if cfg.llm_provider == "ollama":
        return OllamaRuntime(cfg).readiness_sync().as_check()
    if cfg.llm_provider == "mock" and cfg.app_env in {"development", "test"}:
        return OllamaReadiness(
            ok=True,
            model_identity=ModelIdentity(
                provider="mock",
                model="deterministic-test",
                local=True,
            ),
        ).as_check()
    return {
        "ok": False,
        "provider": cfg.llm_provider,
        "model": cfg.llm_model[:128],
        "local": False,
        "digest": None,
        "reason_code": LLMReasonCode.MODEL_NOT_LOCAL.value,
    }


def readiness_report(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
) -> dict[str, Any]:
    """Return bounded dependency status without exposing connection details."""
    cfg = settings or get_settings()
    checks: dict[str, dict[str, Any]] = {}

    try:
        database_engine = engine or get_engine()
        with database_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        expected = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        checks["database"] = {"ok": True}
        checks["migration"] = {"ok": current == expected}
    except Exception:
        checks["database"] = {"ok": False}
        checks["migration"] = {"ok": False}

    try:
        checks["redis"] = {"ok": bool(redis_client(cfg).ping())}
        checks["worker"] = _heartbeat_status("worker", cfg)
        checks["beat"] = _heartbeat_status("beat", cfg)
    except Exception:
        checks["redis"] = {"ok": False}
        checks["worker"] = {"ok": False, "detail": "unavailable"}
        checks["beat"] = {"ok": False, "detail": "unavailable"}

    data_dir = cfg.data_dir
    checks["shared_storage"] = {"ok": data_dir.exists() and os.access(data_dir, os.R_OK | os.W_OK)}
    try:
        checks["browser"] = _heartbeat_status("browser", cfg)
    except Exception:
        checks["browser"] = {"ok": False, "detail": "unavailable"}
    try:
        checks["llm"] = llm_readiness(cfg)
    except Exception:
        checks["llm"] = {
            "ok": False,
            "provider": "ollama" if cfg.llm_provider == "ollama" else cfg.llm_provider,
            "model": cfg.llm_model[:128],
            "local": cfg.llm_provider in {"ollama", "mock"},
            "digest": None,
            "reason_code": LLMReasonCode.PROVIDER_UNAVAILABLE.value,
        }
    status = "ready" if all(value["ok"] for value in checks.values()) else "degraded"
    return {"status": status, "checks": checks}
