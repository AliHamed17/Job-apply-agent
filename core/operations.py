"""Production operations primitives: rate limits, heartbeats, and readiness."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

import redis
from sqlalchemy import text

from core.config import Settings, get_settings
from db.session import get_engine

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
    redis_client(settings).set(f"{HEARTBEAT_PREFIX}{component}", str(time.time()), ex=3600)


def _heartbeat_status(component: str, settings: Settings) -> dict[str, Any]:
    raw = redis_client(settings).get(f"{HEARTBEAT_PREFIX}{component}")
    if not raw:
        return {"ok": False, "detail": "missing"}
    age = max(0.0, time.time() - float(str(raw)))
    return {
        "ok": age <= settings.dependency_heartbeat_ttl_seconds,
        "age_seconds": round(age, 1),
    }


def browser_available() -> bool:
    if shutil.which("chromium") or shutil.which("chromium-browser"):
        return True
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", Path.home() / ".cache/ms-playwright"))
    return cache.exists() and any(cache.glob("chromium-*"))


def readiness_report(settings: Settings | None = None) -> dict[str, Any]:
    """Return bounded dependency status without exposing connection details."""
    cfg = settings or get_settings()
    checks: dict[str, dict[str, Any]] = {}

    try:
        with get_engine().connect() as connection:
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
    checks["shared_storage"] = {
        "ok": data_dir.exists() and os.access(data_dir, os.R_OK | os.W_OK)
    }
    try:
        checks["browser"] = _heartbeat_status("browser", cfg)
    except Exception:
        checks["browser"] = {"ok": False, "detail": "unavailable"}
    status = "ready" if all(value["ok"] for value in checks.values()) else "degraded"
    return {"status": status, "checks": checks}
