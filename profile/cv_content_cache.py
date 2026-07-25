"""In-memory cache for extracted CV text by CV ID or PDF path."""

from __future__ import annotations

from pathlib import Path
from profile.cv_routing import load_routing_config
from profile.pdf_loader import extract_text_from_pdf

import structlog

from core.config import get_settings

logger = structlog.get_logger(__name__)

_CV_TEXT_CACHE: dict[str, str] = {}


def get_cv_text_by_id(
    cv_id: str,
    cv_routing_path: str | Path | None = None,
    cv_directory: str | Path | None = None,
) -> str:
    """Retrieve extracted text for a specific CV ID defined in cv_routing.yaml."""
    if cv_id in _CV_TEXT_CACHE:
        return _CV_TEXT_CACHE[cv_id]

    settings = get_settings()
    routing_path = Path(cv_routing_path or settings.cv_routing_path)
    directory = Path(cv_directory or settings.cv_directory)

    if not routing_path.exists():
        logger.warning("cv_routing_config_missing", path=str(routing_path))
        return ""

    config = load_routing_config(routing_path)
    cv_def = next((cv for cv in config.cvs if cv.id == cv_id), None)
    if not cv_def:
        logger.warning("cv_id_not_found_in_routing", cv_id=cv_id)
        return ""

    pdf_path = (directory / cv_def.file).resolve()
    if not pdf_path.exists():
        logger.warning("cv_pdf_file_missing", cv_id=cv_id, path=str(pdf_path))
        return ""

    try:
        text = extract_text_from_pdf(pdf_path)
        _CV_TEXT_CACHE[cv_id] = text
        logger.info("cv_text_cached", cv_id=cv_id, length=len(text))
        return text
    except Exception as exc:
        logger.error("cv_text_extraction_error", cv_id=cv_id, path=str(pdf_path), error=str(exc))
        return ""


def get_cv_text_by_path(pdf_path: str | Path) -> str:
    """Retrieve extracted text directly from a PDF path, caching the result."""
    path_key = str(Path(pdf_path).resolve())
    if path_key in _CV_TEXT_CACHE:
        return _CV_TEXT_CACHE[path_key]

    p = Path(pdf_path)
    if not p.exists():
        logger.warning("pdf_path_missing", path=path_key)
        return ""

    try:
        text = extract_text_from_pdf(p)
        _CV_TEXT_CACHE[path_key] = text
        return text
    except Exception as exc:
        logger.error("pdf_extraction_error", path=path_key, error=str(exc))
        return ""


def clear_cv_text_cache() -> None:
    """Clear all cached CV texts."""
    _CV_TEXT_CACHE.clear()
