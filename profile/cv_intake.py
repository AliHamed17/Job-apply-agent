"""Validated, bounded and atomic CV ingestion shared by API and WhatsApp."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from profile.builder import build_profile_from_pdf
from profile.loader import load_profile
from profile.models import UserProfile
from profile.writer import save_profile

import structlog

from worker.rescore import rescore_pending_jobs

logger = structlog.get_logger(__name__)
_ingest_lock = asyncio.Lock()


class CVIngestError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(f"{code}: {message}")


def _merge_operator_profile_state(
    rebuilt: UserProfile,
    existing: UserProfile,
) -> UserProfile:
    """Preserve manual state while binding new facts to the uploaded CV."""

    merged = rebuilt.model_copy(deep=True)
    for field_name in ("name", "email", "phone", "location"):
        existing_value = str(getattr(existing.personal, field_name) or "").strip()
        if existing_value:
            setattr(merged.personal, field_name, existing_value)
    for field_name in ("linkedin", "github", "portfolio"):
        existing_value = str(getattr(existing.links, field_name) or "").strip()
        if existing_value:
            setattr(merged.links, field_name, existing_value)

    merged.evidence.user_confirmed = dict(existing.evidence.user_confirmed)
    artifact_facts = {
        digest: dict(facts) for digest, facts in existing.evidence.cv_extracted_by_artifact.items()
    }
    artifact_facts.update(
        {digest: dict(facts) for digest, facts in rebuilt.evidence.cv_extracted_by_artifact.items()}
    )
    merged.evidence.cv_extracted_by_artifact = artifact_facts

    merged.cover_letter = existing.cover_letter.model_copy(deep=True)
    merged.attachments = [item.model_copy(deep=True) for item in existing.attachments]
    for field_name in (
        "remote_ok",
        "hybrid_ok",
        "onsite_ok",
        "salary",
        "blacklist_companies",
    ):
        existing_value = getattr(existing.preferences, field_name)
        setattr(
            merged.preferences,
            field_name,
            (
                existing_value.model_copy(deep=True)
                if hasattr(existing_value, "model_copy")
                else list(existing_value)
                if isinstance(existing_value, list)
                else existing_value
            ),
        )
    return merged


def _validate_pdf(path: Path, max_bytes: int) -> None:
    size = path.stat().st_size
    if not size:
        raise CVIngestError("EMPTY_FILE", "The uploaded file is empty.")
    if size > max_bytes:
        raise CVIngestError("TOO_LARGE", "The file is larger than the allowed limit.")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise CVIngestError("NOT_PDF", "The file is not a valid PDF.")
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise CVIngestError("ENCRYPTED_PDF", "Password-protected PDFs are unsupported.")
        if not reader.pages:
            raise CVIngestError("CORRUPT_PDF", "The PDF has no readable pages.")
    except CVIngestError:
        raise
    except Exception as exc:
        raise CVIngestError("CORRUPT_PDF", "The PDF could not be read.") from exc


async def stream_to_temp(
    read_chunk: Callable[[], Awaitable[bytes]], directory: Path, max_bytes: int
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=directory, suffix=".pdf.part")
    path, total = Path(name), 0
    try:
        with os.fdopen(fd, "wb") as output:
            while chunk := await read_chunk():
                total += len(chunk)
                if total > max_bytes:
                    raise CVIngestError("TOO_LARGE", "The file is larger than the allowed limit.")
                output.write(chunk)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def bytes_to_temp(data: bytes, directory: Path, max_bytes: int) -> Path:
    if len(data) > max_bytes:
        raise CVIngestError("TOO_LARGE", "The file is larger than the allowed limit.")
    directory.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=directory, suffix=".pdf.part")
    with os.fdopen(fd, "wb") as output:
        output.write(data)
    return Path(name)


async def ingest_cv_from_temp(tmp_pdf: Path, *, settings, db, max_bytes: int) -> dict:
    async with _ingest_lock:
        try:
            _validate_pdf(tmp_pdf, max_bytes)
            existing = (
                load_profile(settings.profile_path) if settings.profile_path.exists() else None
            )
            profile = await build_profile_from_pdf(str(tmp_pdf))
            if existing is not None:
                profile = _merge_operator_profile_state(profile, existing)
        except CVIngestError:
            tmp_pdf.unlink(missing_ok=True)
            raise
        except Exception as exc:
            tmp_pdf.unlink(missing_ok=True)
            logger.warning("cv_ingest_parse_failed", error=type(exc).__name__)
            raise CVIngestError("PARSE_FAILED", "Could not build a profile from the CV.") from exc

        final_pdf = settings.resume_path
        final_pdf.parent.mkdir(parents=True, exist_ok=True)
        old_pdf = final_pdf.read_bytes() if final_pdf.exists() else None
        old_yaml = settings.profile_path.read_bytes() if settings.profile_path.exists() else None
        profile.resume.pdf_path = str(final_pdf)
        os.replace(tmp_pdf, final_pdf)
        try:
            version = save_profile(profile, settings.profile_path, db=db)
        except Exception:
            if old_pdf is None:
                final_pdf.unlink(missing_ok=True)
            else:
                final_pdf.write_bytes(old_pdf)
            if old_yaml is None:
                settings.profile_path.unlink(missing_ok=True)
            else:
                settings.profile_path.write_bytes(old_yaml)
            raise
        rescored = rescore_pending_jobs(db, profile) if db is not None else 0
        logger.info("cv_ingested", version=version, rescored=rescored)
        return {
            "version": version,
            "name": profile.personal.name,
            "roles": profile.preferences.roles,
            "keywords_count": len(profile.preferences.keywords),
            "rescored": rescored,
        }
