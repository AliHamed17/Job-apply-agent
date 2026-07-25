"""Three-layer answer resolver for Easy Apply form fields."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import structlog

from llm.client import LLMClient, get_llm_client

logger = structlog.get_logger(__name__)

_UNKNOWN = "UNKNOWN"
_SENSITIVE_TERMS = (
    "work authorization",
    "authorized to work",
    "visa",
    "sponsorship",
    "nationality",
    "citizen",
    "citizenship",
    "security clearance",
    "certification",
    "license",
    "terms",
    "consent",
    "attest",
    "certify",
    "privacy policy",
    "gender",
    "race",
    "ethnicity",
    "veteran",
    "disability",
    "demographic",
)


def normalize_question(q: str) -> str:
    q = (q or "").lower().strip()
    q = re.sub(r"[^\w\s]", "", q)
    return re.sub(r"\s+", " ", q)


def question_hash(q: str) -> str:
    return hashlib.sha256(normalize_question(q).encode()).hexdigest()


def is_sensitive_question(label: str) -> bool:
    """Whether a question requires explicit user-confirmed evidence."""
    normalized = normalize_question(label)
    return any(term in normalized for term in _SENSITIVE_TERMS)


@dataclass
class FieldSpec:
    label: str
    kind: str  # text|number|select|radio|checkbox|file|textarea
    options: list[str] = field(default_factory=list)
    required: bool = False


@dataclass
class AnswerResult:
    value: str | None
    source: str  # deterministic | cache | llm
    confident: bool


class FormBrain:
    def __init__(
        self,
        profile,
        client: LLMClient | None = None,
        db=None,
        cv_text: str | None = None,
        selected_cv_id: str | None = None,
    ):
        self.profile = profile
        self.client = client
        self.db = db
        self.selected_cv_id = selected_cv_id
        self._cv_text = cv_text

    # ── layer 1: deterministic map ────────────────────
    def _deterministic(self, label: str) -> str | None:
        p = self.profile
        low = label.lower()
        table = [
            (("email",), p.personal.email),
            (("first name",), (p.personal.name.split()[0] if p.personal.name else "")),
            (
                ("last name", "surname"),
                " ".join(p.personal.name.split()[1:]) if p.personal.name else "",
            ),
            (("full name", "your name"), p.personal.name),
            (("phone", "mobile"), p.personal.phone),
            (
                ("city", "location"),
                p.personal.location.split(",")[0].strip() if p.personal.location else "",
            ),
            (("linkedin",), p.links.linkedin),
            (("github",), p.links.github),
            (("portfolio", "website"), p.links.portfolio),
        ]
        for keys, val in table:
            if any(k in low for k in keys) and val:
                return val
        return None

    def _confirmed_sensitive(self, label: str) -> AnswerResult | None:
        normalized = normalize_question(label)
        if not is_sensitive_question(label):
            return None
        confirmed = self.profile.evidence.user_confirmed
        for key, value in confirmed.items():
            normalized_key = normalize_question(key)
            if normalized_key in normalized or normalized in normalized_key:
                return AnswerResult(value, "user_confirmed", True)
        return AnswerResult(None, "confirmed_evidence_required", False)

    # ── layer 2: cache ────────────────────────────────
    def _cache_get(self, qh: str) -> str | None:
        if self.db is None:
            return None
        from db.models import AnswerCache  # noqa: PLC0415

        row = self.db.query(AnswerCache).filter(AnswerCache.question_hash == qh).first()
        return row.answer if row else None

    def _cache_put(self, qh: str, label: str, answer: str, source: str) -> None:
        if self.db is None:
            return
        from db.models import AnswerCache  # noqa: PLC0415

        if self.db.query(AnswerCache).filter(AnswerCache.question_hash == qh).first():
            return
        self.db.add(
            AnswerCache(question_hash=qh, question_text=label, answer=answer, source=source)
        )
        self.db.commit()

    # ── layer 3: LLM ──────────────────────────────────
    async def _llm(self, fspec: FieldSpec, job) -> str:
        client = self.client or get_llm_client()
        opts = f"\nChoose exactly one of: {fspec.options}" if fspec.options else ""
        job_ctx = (
            f"\nJob: {getattr(job, 'title', '')} at {getattr(job, 'company', '')}" if job else ""
        )
        if self._cv_text and self._cv_text.strip():
            cv_text = self._cv_text
        elif self.selected_cv_id:
            from profile.cv_content_cache import get_cv_text_by_id

            cv_text = get_cv_text_by_id(self.selected_cv_id)
        else:
            cv_text = self.profile.resume.text

        prompt = (
            "Answer this job-application question using ONLY the candidate CV. "
            f"If the CV does not support a confident answer, reply exactly '{_UNKNOWN}'. "
            "Never invent certifications, visas, or clearances.\n"
            f"Question: {fspec.label}{opts}{job_ctx}\n\nCV:\n{cv_text[:4000]}"
        )
        return (await client.generate(prompt=prompt, max_tokens=120, temperature=0.0)).strip()

    async def answer(self, field: FieldSpec, job) -> AnswerResult:
        qh = question_hash(field.label)

        sensitive = self._confirmed_sensitive(field.label)
        if sensitive is not None:
            return sensitive

        det = self._deterministic(field.label)
        if det:
            return AnswerResult(det, "deterministic", True)

        cached = self._cache_get(qh)
        if cached is not None:
            return AnswerResult(cached, "cache", True)

        raw = await self._llm(field, job)
        if not raw or raw.upper() == _UNKNOWN:
            return AnswerResult(None, "llm", False)

        self._cache_put(qh, field.label, raw, "llm")
        return AnswerResult(raw, "llm", True)
