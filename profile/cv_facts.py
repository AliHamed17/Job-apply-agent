"""Literal-source fact catalogs for one content-addressed selected CV."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from profile.models import (
    CVArtifact,
    CVArtifactFactV1,
    SelectedCVFactCatalog,
)

_FACT_LABELS: dict[str, tuple[str, ...]] = {
    "primary_language": ("primary programming language",),
    "backend_framework": ("backend framework",),
    "database_skill": ("database", "database technology"),
    "cloud_platform": ("cloud", "cloud platform"),
    "container_platform": ("containers", "container platform"),
    "iac_tool": ("infrastructure as code tool", "iac tool"),
    "data_tool": ("distributed data tool",),
    "ml_framework": ("machine learning framework", "ml framework"),
    "frontend_language": ("frontend language",),
    "frontend_framework": ("frontend framework",),
    "test_framework": ("testing framework", "test framework"),
    "automation_tool": ("browser automation tool", "automation tool"),
    "operating_system": ("operating system",),
    "embedded_language": ("embedded programming language",),
    "realtime_system": ("real time operating system", "rtos"),
    "analytics_tool": ("analytics visualization tool", "analytics tool"),
    "pipeline_tool": ("data pipeline tool", "pipeline tool"),
    "api_style": ("api design style", "api style"),
    "version_control": ("version control system", "version control"),
    "highest_degree": ("highest academic degree", "highest degree"),
    "relevant_experience": ("relevant experience",),
    "technical_summary": ("technical summary",),
}
_EXPERIENCE_FACT_KEYS = frozenset({"relevant_experience", "technical_summary"})
_MULTI_VALUE_RE = re.compile(
    r"[,;/|]|\s[&+]\s|\b(?:and|or)\b|"
    r"(?<![\w\u0590-\u05ff])ו(?=[\u0590-\u05ff])",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"\s+")
_BULLET_PREFIX_RE = re.compile(r"^(?:[-*•▪◦‣]\s*)+")
_CANDIDATE_EXPERIENCE_RE = re.compile(
    r"^(?:"
    r"(?:(?:i|the\s+candidate|candidate)\s+)?"
    r"(?:built|created|delivered|designed|developed|drove|implemented|"
    r"improved|launched|led|managed|optimized|owned|reduced|scaled|"
    r"studied|worked|architected|automated|deployed|maintained|tested)|"
    r"(?:responsible\s+for|experienced\s+(?:in|with)|skilled\s+in|"
    r"proficient\s+in)"
    r")\b",
    re.IGNORECASE,
)


def _normalized_source_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value))
    normalized = "".join(
        character for character in normalized if unicodedata.category(character) != "Cf"
    )
    return _SPACE_RE.sub(" ", normalized).strip().casefold()


def _source_segments(cv_text: str) -> tuple[str, ...]:
    """Return stable complete source lines/sentences without fabricating text."""

    segments: list[str] = []
    seen: set[str] = set()
    for raw_line in re.split(r"[\r\n]+", cv_text):
        line = raw_line.strip()
        if not line:
            continue
        candidates = (line, *re.split(r"(?<=[.!?])\s+", line))
        for candidate in candidates:
            clean = candidate.strip()
            normalized = _normalized_source_text(clean)
            if normalized and normalized not in seen:
                seen.add(normalized)
                segments.append(clean)
    return tuple(segments)


def _labeled_value(
    canonical_name: str,
    source_quote: str,
) -> str | None:
    labels = _FACT_LABELS.get(canonical_name)
    if not labels:
        return None
    label_pattern = "|".join(re.escape(label) for label in labels)
    clean_quote = _BULLET_PREFIX_RE.sub("", source_quote).strip()
    match = re.fullmatch(
        rf"(?:{label_pattern})\s*[:：=\-–—]\s*(.+?)\s*[.;]?",
        clean_quote,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def cv_fact_is_literal_source_bound(
    fact: CVArtifactFactV1,
    cv_text: str,
) -> bool:
    """Prove a canonical value from one exact, explicitly labelled source span."""

    normalized_segments = {_normalized_source_text(item) for item in _source_segments(cv_text)}
    if _normalized_source_text(fact.source_quote) not in normalized_segments:
        return False
    labeled_value = _labeled_value(fact.canonical_name, fact.source_quote)
    if labeled_value is None or _normalized_source_text(fact.value) != _normalized_source_text(
        labeled_value
    ):
        return False
    if fact.canonical_name in _EXPERIENCE_FACT_KEYS:
        clean = _BULLET_PREFIX_RE.sub("", fact.value).strip()
        return bool(clean and _CANDIDATE_EXPERIENCE_RE.match(clean))
    return _MULTI_VALUE_RE.search(labeled_value) is None


def _candidate_facts(
    segments: Iterable[str],
) -> tuple[CVArtifactFactV1, ...]:
    candidates: dict[str, dict[str, CVArtifactFactV1]] = {}
    for source_quote in segments:
        for canonical_name in _FACT_LABELS:
            value = _labeled_value(canonical_name, source_quote)
            if value is None:
                continue
            try:
                fact = CVArtifactFactV1(
                    canonical_name=canonical_name,
                    value=value,
                    source_quote=source_quote,
                )
            except ValueError:
                continue
            if not cv_fact_is_literal_source_bound(fact, "\n".join(segments)):
                continue
            normalized_value = _normalized_source_text(value)
            candidates.setdefault(canonical_name, {})[normalized_value] = fact
    return tuple(
        next(iter(values.values()))
        for canonical_name, values in sorted(candidates.items())
        if len(values) == 1
    )


def build_selected_cv_fact_catalog(artifact: CVArtifact) -> SelectedCVFactCatalog:
    """Build a private immutable catalog without mutating profile facts."""

    segments = _source_segments(artifact.extracted_text)
    return SelectedCVFactCatalog(
        artifact=artifact,
        facts=_candidate_facts(segments),
    )
