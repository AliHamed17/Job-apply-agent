"""User profile loader — reads and validates user_profile.yaml."""

from __future__ import annotations

from pathlib import Path
from profile.models import UserProfile

import structlog
import yaml

from core.config import get_settings

logger = structlog.get_logger(__name__)

_profile: UserProfile | None = None


def load_profile(path: Path | None = None) -> UserProfile:
    """Load user profile from YAML file.

    Validates against the Pydantic model and caches the result.
    """
    global _profile
    if _profile is not None:
        return _profile

    if path is None:
        path = get_settings().profile_path

    if not path.exists():
        logger.warning("profile_not_found", path=str(path))
        _profile = UserProfile()
        return _profile

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        _profile = UserProfile(**raw)
        logger.info("profile_loaded", name=_profile.personal.name, path=str(path))
        return _profile
    except Exception as exc:
        logger.error("profile_load_error", path=str(path), error=str(exc))
        raise ValueError(f"Failed to load user profile from {path}: {exc}") from exc


def get_profile() -> UserProfile:
    """Get the cached profile (loads on first call)."""
    return load_profile()


def reload_profile() -> UserProfile:
    """Force-reload the profile from disk."""
    global _profile
    _profile = None
    return load_profile()
