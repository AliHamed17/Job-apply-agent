"""User profile loader — reads and validates user_profile.yaml.

If ``resume.pdf_path`` is set and ``resume.text`` is empty the loader
automatically extracts text from the PDF using :mod:`profile.pdf_loader`.
"""

from __future__ import annotations

from pathlib import Path
from profile.models import UserProfile

import structlog
import yaml

from core.config import get_settings

logger = structlog.get_logger(__name__)

_profile: UserProfile | None = None


def _read_profile(path: Path) -> UserProfile:
    """Read one exact YAML snapshot without consulting or changing the cache."""

    if not path.exists():
        logger.warning("profile_not_found", path=str(path))
        return UserProfile()

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        profile = UserProfile(**raw)

        # ── Auto-populate resume text from PDF ──────────────────────────
        if profile.resume.pdf_path and not profile.resume.text.strip():
            pdf_path = Path(profile.resume.pdf_path)
            if not pdf_path.is_absolute():
                # Resolve relative to the YAML file's directory
                pdf_path = path.parent / pdf_path
            try:
                from profile.pdf_loader import extract_text_from_pdf  # noqa: PLC0415

                extracted = extract_text_from_pdf(pdf_path)
                if extracted.strip():
                    resume_data = profile.resume.model_dump()
                    resume_data["text"] = extracted
                    profile_data = profile.model_dump()
                    profile_data["resume"] = resume_data
                    profile = UserProfile(**profile_data)
                    logger.info(
                        "resume_text_from_pdf",
                        pdf=str(pdf_path),
                        chars=len(extracted),
                    )
                else:
                    logger.warning("pdf_empty_text", pdf=str(pdf_path))
            except (FileNotFoundError, ImportError) as exc:
                logger.warning("pdf_load_skipped", pdf=str(pdf_path), reason=str(exc))

        logger.info("profile_loaded", name=profile.personal.name, path=str(path))
        return profile
    except Exception as exc:
        logger.error("profile_load_error", path=str(path), error=str(exc))
        raise ValueError(f"Failed to load user profile from {path}: {exc}") from exc


def load_profile_snapshot(path: Path) -> UserProfile:
    """Read the latest exact file for a serialized read-modify-write operation."""

    return _read_profile(Path(path))


def load_profile(path: Path | None = None) -> UserProfile:
    """Load and cache the configured user profile."""

    global _profile
    if _profile is not None:
        return _profile
    resolved_path = Path(path) if path is not None else get_settings().profile_path
    _profile = _read_profile(resolved_path)
    return _profile


def get_profile() -> UserProfile:
    """Get the cached profile (loads on first call)."""
    return load_profile()


def reload_profile() -> UserProfile:
    """Force-reload the profile from disk."""
    global _profile
    _profile = None
    return load_profile()


def set_profile(profile: UserProfile) -> None:
    """Replace the cached profile (used after an in-place rebuild)."""
    global _profile
    _profile = profile
