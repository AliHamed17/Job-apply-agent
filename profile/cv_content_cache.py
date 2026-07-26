"""Content-addressed cache for immutable CV artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from hmac import compare_digest
from pathlib import Path
from profile.cv_routing import CVRoutingConfig, load_routing_config
from profile.models import CVArtifact, SelectedCVArtifact
from profile.pdf_loader import extract_text_from_pdf

import structlog

from core.config import get_settings

logger = structlog.get_logger(__name__)

_CV_ARTIFACT_CACHE: dict[str, CVArtifact] = {}
_HASH_CHUNK_BYTES = 1024 * 1024


class CVArtifactBindingError(RuntimeError):
    """The selected local CV no longer matches the reviewed byte identity."""


def _pdf_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_cv_artifact_by_path(pdf_path: str | Path) -> CVArtifact | None:
    """Return a PDF extraction bound to the exact source bytes.

    The digest is recomputed before every lookup.  Replacing a PDF at the same
    path therefore cannot reuse stale extracted text from the old document.
    """

    path = Path(pdf_path).resolve()
    if not path.is_file():
        logger.warning("pdf_path_missing")
        return None

    try:
        pdf_sha256 = _pdf_sha256(path)
    except OSError as exc:
        logger.error("pdf_hash_error", error=type(exc).__name__)
        return None

    cached = _CV_ARTIFACT_CACHE.get(pdf_sha256)
    if cached is not None:
        return cached

    try:
        text = extract_text_from_pdf(path)
    except Exception as exc:
        logger.error(
            "pdf_extraction_error",
            cv_digest_prefix=pdf_sha256[:12],
            error=type(exc).__name__,
        )
        return None

    try:
        verified_sha256 = _pdf_sha256(path)
        byte_size = path.stat().st_size
    except OSError as exc:
        logger.error("pdf_post_extract_verification_error", error=type(exc).__name__)
        return None
    if verified_sha256 != pdf_sha256:
        logger.warning("cv_source_changed_during_extraction")
        return None

    artifact = CVArtifact(
        pdf_sha256=pdf_sha256,
        byte_size=byte_size,
        extracted_text=text,
    )
    _CV_ARTIFACT_CACHE[pdf_sha256] = artifact
    logger.info(
        "cv_artifact_cached",
        cv_digest_prefix=pdf_sha256[:12],
        byte_size=artifact.byte_size,
        extracted_chars=len(text),
    )
    return artifact


def get_selected_cv_artifact_by_id(
    cv_id: str,
    cv_routing_path: str | Path | None = None,
    cv_directory: str | Path | None = None,
) -> SelectedCVArtifact | None:
    """Resolve a configured CV ID to an immutable local artifact binding."""

    settings = get_settings()
    routing_path = Path(cv_routing_path or settings.cv_routing_path)
    directory = Path(cv_directory or settings.cv_directory).resolve()

    if not routing_path.exists():
        logger.warning("cv_routing_config_missing")
        return None

    config = load_routing_config(routing_path)
    cv_def = next((cv for cv in config.cvs if cv.id == cv_id), None)
    if not cv_def:
        logger.warning("cv_id_not_found_in_routing", cv_id=cv_id)
        return None

    pdf_path = (directory / cv_def.file).resolve()
    try:
        pdf_path.relative_to(directory)
    except ValueError:
        logger.error("cv_pdf_path_outside_directory", cv_id=cv_id)
        return None
    if not pdf_path.is_file():
        logger.warning("cv_pdf_file_missing", cv_id=cv_id)
        return None
    artifact = get_cv_artifact_by_path(pdf_path)
    if artifact is None:
        return None
    return SelectedCVArtifact(
        cv_id=cv_id,
        resolved_path=str(pdf_path),
        artifact=artifact,
    )


def load_configured_cv_artifacts(
    config: CVRoutingConfig,
    cv_directory: str | Path,
) -> Mapping[str, SelectedCVArtifact]:
    """Resolve every readable configured CV once from one config snapshot."""

    directory = Path(cv_directory).resolve()
    resolved: dict[str, SelectedCVArtifact] = {}
    for cv in config.cvs:
        pdf_path = (directory / cv.file).resolve()
        try:
            pdf_path.relative_to(directory)
        except ValueError:
            logger.error("cv_pdf_path_outside_directory", cv_id=cv.id)
            continue
        artifact = get_cv_artifact_by_path(pdf_path)
        if artifact is None:
            continue
        resolved[cv.id] = SelectedCVArtifact(
            cv_id=cv.id,
            resolved_path=str(pdf_path),
            artifact=artifact,
        )
    return resolved


def require_current_selected_cv_artifact(
    selected: SelectedCVArtifact,
    *,
    expected_sha256: str | None = None,
) -> SelectedCVArtifact:
    """Return the same immutable binding only while its source path is unchanged."""

    expected = expected_sha256 or selected.pdf_sha256
    if not compare_digest(expected, selected.pdf_sha256):
        raise CVArtifactBindingError("CV_ARTIFACT_BINDING_MISMATCH")
    try:
        current_sha256 = _pdf_sha256(Path(selected.resolved_path))
    except OSError as exc:
        raise CVArtifactBindingError("CV_ARTIFACT_UNAVAILABLE") from exc
    if not compare_digest(current_sha256, selected.pdf_sha256):
        raise CVArtifactBindingError("CV_ARTIFACT_CHANGED")
    return selected


def get_cv_artifact_by_id(
    cv_id: str,
    cv_routing_path: str | Path | None = None,
    cv_directory: str | Path | None = None,
) -> CVArtifact | None:
    """Return only the content-addressed portion of a configured CV."""

    selected = get_selected_cv_artifact_by_id(
        cv_id,
        cv_routing_path=cv_routing_path,
        cv_directory=cv_directory,
    )
    return selected.artifact if selected is not None else None


def get_cv_text_by_id(
    cv_id: str,
    cv_routing_path: str | Path | None = None,
    cv_directory: str | Path | None = None,
) -> str:
    """Backward-compatible text view over the immutable CV artifact."""

    artifact = get_cv_artifact_by_id(
        cv_id,
        cv_routing_path=cv_routing_path,
        cv_directory=cv_directory,
    )
    return artifact.extracted_text if artifact is not None else ""


def get_cv_text_by_path(pdf_path: str | Path) -> str:
    """Backward-compatible text view for a direct local PDF path."""

    artifact = get_cv_artifact_by_path(pdf_path)
    return artifact.extracted_text if artifact is not None else ""


def clear_cv_text_cache() -> None:
    """Clear all content-addressed CV artifacts."""

    _CV_ARTIFACT_CACHE.clear()
