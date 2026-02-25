"""PDF resume parser — extracts plain text from a PDF resume.

Supports two extraction backends (tried in order):
1. **pypdf** (pure-Python, zero system deps) — fast for text-based PDFs.
2. **pdfminer.six** (layout-aware) — better for complex multi-column layouts.

If neither is installed the function raises a clear ImportError.
If the PDF contains only scanned images (no embedded text) both backends
will return an empty string; the caller should fall back to the YAML
``resume.text`` field in that case.

Usage::

    from profile.pdf_loader import extract_text_from_pdf

    text = extract_text_from_pdf("path/to/resume.pdf")
"""

from __future__ import annotations

import io
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


def extract_text_from_pdf(path: str | Path) -> str:
    """Extract plain text from a PDF resume file.

    Tries pypdf first (lightweight), falls back to pdfminer.six for
    complex layouts.  Returns an empty string if the file cannot be read
    or contains no extractable text (e.g. scanned image-only PDFs).

    Args:
        path: Absolute or relative path to the PDF file.

    Returns:
        Extracted text as a single string with newlines preserved.

    Raises:
        FileNotFoundError: If the PDF file does not exist.
        ImportError: If neither pypdf nor pdfminer.six is installed.
    """
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF resume not found: {pdf_path}")

    # ── Backend 1: pypdf ──────────────────────────────────────────────────
    text = _extract_with_pypdf(pdf_path)
    if text.strip():
        logger.info("pdf_extracted_pypdf", path=str(pdf_path), chars=len(text))
        return text

    # ── Backend 2: pdfminer.six ───────────────────────────────────────────
    text = _extract_with_pdfminer(pdf_path)
    if text.strip():
        logger.info("pdf_extracted_pdfminer", path=str(pdf_path), chars=len(text))
        return text

    logger.warning("pdf_no_text_extracted", path=str(pdf_path))
    return ""


def _extract_with_pypdf(path: Path) -> str:
    """Extract text using the pypdf library."""
    try:
        import pypdf  # noqa: PLC0415
    except ImportError:
        try:
            import PyPDF2 as pypdf  # type: ignore[no-redef]  # legacy name
        except ImportError:
            return ""  # library not installed, fall through to pdfminer

    try:
        reader = pypdf.PdfReader(str(path))
        pages: list[str] = []
        for page in reader.pages:
            page_text = page.extract_text() or ""
            pages.append(page_text)
        return "\n".join(pages)
    except Exception as exc:
        logger.warning("pypdf_extraction_failed", path=str(path), error=str(exc))
        return ""


def _extract_with_pdfminer(path: Path) -> str:
    """Extract text using pdfminer.six (layout-aware)."""
    try:
        from pdfminer.high_level import extract_text as pm_extract  # noqa: PLC0415
    except ImportError:
        raise ImportError(
            "No PDF extraction library found. "
            "Install at least one: pip install pypdf  OR  pip install pdfminer.six"
        )

    try:
        with open(path, "rb") as f:
            return pm_extract(io.BytesIO(f.read()))
    except Exception as exc:
        logger.warning("pdfminer_extraction_failed", path=str(path), error=str(exc))
        return ""
