"""Language detection helper for multilingual job postings (Hebrew/English)."""

from __future__ import annotations

import re

HEBREW_CHAR_PATTERN = re.compile(r"[\u0590-\u05FF]")


def detect_language(text: str) -> str:
    """Detect primary language of job text.

    Returns:
        'he' if text contains significant Hebrew characters (>15% of word chars),
        'en' otherwise.
    """
    if not text:
        return "en"
    hebrew_count = len(HEBREW_CHAR_PATTERN.findall(text))
    total_letters = len(re.findall(r"\w", text))
    if total_letters > 0 and (hebrew_count / total_letters) > 0.15:
        return "he"
    return "en"
