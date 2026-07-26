"""Deterministic claim-to-evidence validation for generated materials.

The local model may propose prose, but it cannot make its own prose eligible.
Every candidate-specific factual sentence must cite immutable evidence and pass
these deterministic checks.  Over-abstention is intentional: an operator can
edit/review a blocked draft, while an unsupported claim must never become
submission-eligible.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from profile.models import CVArtifact, UserProfile, is_sensitive_fact_key
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.sensitive_policy import contains_prompt_injection, contains_sensitive_text

ClaimBlocker = Literal[
    "CLAIM_NOT_IN_MATERIAL",
    "CLAIM_EVIDENCE_MISSING",
    "CLAIM_EVIDENCE_UNKNOWN",
    "CLAIM_EVIDENCE_MISMATCH",
    "SENSITIVE_CLAIM_PROHIBITED",
    "UNDECLARED_FACTUAL_CLAIM",
    "PROHIBITED_GENERATED_CONTENT",
]

_NUMBER_RE = re.compile(r"(?<!\w)\d+(?:[.,]\d+)?%?(?!\w)")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+|\n+")
_SPACE_RE = re.compile(r"\s+")
_FIRST_PERSON = re.compile(r"\b(?:I|I've|I’m|I'm|my|mine|me)\b", re.IGNORECASE)
_HEBREW_FIRST_PERSON = re.compile(r"(?<![\w\u0590-\u05ff])(?:אני|שלי|יש\s+לי)(?![\w\u0590-\u05ff])")
_IMPLIED_CANDIDATE = re.compile(
    r"^(?:an?\s+)?(?:experienced|skilled|proficient|certified|licensed)\b|"
    r"^with\s+\d+(?:[.,]\d+)?\s+years?\b",
    re.IGNORECASE,
)
_FACTUAL_MARKER = re.compile(
    r"\b("
    r"built|created|delivered|designed|developed|drove|earned|implemented|improved|"
    r"launched|led|managed|optimized|owned|reduced|scaled|studied|worked|"
    r"background|degree|experience|expertise|skill|proficien|years?|%|"
    r"possess|holds?|has|phd|doctorate|bachelor|master(?:'s)?|seasoned|leader|"
    r"weeks?|months?|days?|salary|compensation|notice|available|immediate|"
    r"based|located|relocat|start\s+date"
    r")\b",
    re.IGNORECASE,
)
_THIRD_PERSON_CANDIDATE = re.compile(
    r"^(?!(?:The|This|That|Our|Your|Company|Role|Team|Job)\b)"
    r"(?:[A-Z][A-Za-z'’.-]{1,40})(?:\s+[A-Z][A-Za-z'’.-]{1,40}){0,3}\s+"
    r"(?:has|is|was|brings|possesses|built|created|delivered|designed|developed|"
    r"drove|earned|implemented|improved|launched|led|managed|optimized|owned|"
    r"reduced|scaled|studied|worked)\b"
)
_EMPLOYER_CONTEXT = re.compile(
    r"^(?:the|this|that|our|your)\s+"
    r"(?:company|role|team|job|position|organization|organisation|mission|product)\b",
    re.IGNORECASE,
)
_HEBREW_FACTUAL_MARKER = re.compile(
    r"(?<![\w\u0590-\u05ff])(?:"
    r"ניסיון|שנות?|מהנדס(?:ת)?|מפתח(?:ת)?|ניהל(?:תי|ה)?|הובל(?:תי|ה)?|"
    r"פיתח(?:תי|ה)?|בניתי|בנה|בנתה|יצר(?:תי|ה)?|יישמ(?:תי|ה)?|"
    r"שיפר(?:תי|ה)?|עבד(?:תי|ה)?|למד(?:תי|ה)?|תואר|מומחיות|מיומנות"
    r")(?![\w\u0590-\u05ff])"
)
_SAFE_HEBREW_SUBJECTIVE_FRAGMENT = re.compile(
    r"^(?:"
    r"אני\s+(?:מתרגש|מתרגשת|נלהב|נלהבת)\s+(?:מהתפקיד|מההזדמנות)|"
    r"אני\s+(?:מעוניין|מעוניינת)\s+(?:בתפקיד|בהזדמנות)|"
    r"אשמח\s+לתרום|"
    r"אני\s+מקווה\s+ללמוד\s+עוד\s+על\s+התפקיד"
    r")[.!]?$"
)
_SAFE_SUBJECTIVE_FRAGMENT = re.compile(
    r"^(?:"
    r"i\s+am\s+(?:excited|eager|motivated)\s+about\s+"
    r"(?:this|the)\s+(?:role|opportunity|position)|"
    r"i\s+am\s+interested\s+in\s+(?:(?:learning\s+more\s+about)\s+)?"
    r"(?:this|the)\s+(?:role|opportunity|position)|"
    r"i\s+(?:am\s+eager|would\s+be\s+pleased)\s+to\s+contribute|"
    r"i\s+would\s+welcome\s+the\s+opportunity\s+to\s+contribute|"
    r"i\s+look\s+forward\s+to\s+(?:learning\s+more|"
    r"discussing\s+(?:this|the)\s+(?:role|opportunity|position))"
    r")[.!]?$",
    re.IGNORECASE,
)
_SAFE_NONFACTUAL_FRAGMENT = re.compile(
    r"^(?:"
    r"dear\s+(?:hiring|recruiting|talent|selection)\s+"
    r"(?:team|manager|committee)|"
    r"(?:hello|hi)(?:\s+(?:hiring|recruiting|talent)\s+"
    r"(?:team|manager))?|"
    r"sincerely|best(?:\s+regards)?|kind\s+regards|regards|"
    r"thank\s+you(?:\s+very\s+much)?\s+for\s+(?:your\s+)?"
    r"(?:time|consideration|reviewing\s+my\s+application)|"
    r"thanks\s+for\s+(?:your\s+)?(?:time|consideration)|"
    r"open\s+to\s+discussion|to\s+be\s+discussed|"
    r"best\s+discussed\s+with\s+(?:the\s+)?hiring\s+team"
    r")[.!,:;\s]*$",
    re.IGNORECASE,
)
_CLAUSE_RE = re.compile(
    r"\s+(?:and|but|while|whereas|plus)\s+|[;:]",
    re.IGNORECASE,
)
_NEGATED_EVIDENCE_CONTEXT_RE = re.compile(
    r"\b(?:not|never|no|without|cannot|can't|didn't|doesn't|hasn't|haven't|"
    r"isn't|wasn't|weren't|aren't|lack|lacks|lacked|lacking|deny|denies|"
    r"denied|denying|refute|refutes|refuted|refuting)\b|"
    r"(?<![\w\u0590-\u05ff])(?:לא|מעולם\s+לא|אין|איני|אינני)"
    r"(?![\w\u0590-\u05ff])",
    re.IGNORECASE,
)
_UNCERTAIN_EVIDENCE_CONTEXT_RE = re.compile(
    r"\b(?:allegedly|purportedly|reportedly|supposedly|unverified|disputed|"
    r"possibly|perhaps|uncertain|unclear)\b|"
    r"\b(?:may|might|could)\s+have\b|"
    r"\b(?:cannot|can't|unable\s+to)\s+(?:confirm|verify|substantiate)\b|"
    r"\bit\s+is\s+(?:false|inaccurate|incorrect|untrue)\s+that\b|"
    r"\b(?:the|this|that)\s+(?:claim|statement|assertion|allegation)\b.{0,160}"
    r"\b(?:false|inaccurate|incorrect|untrue|unsubstantiated)\b|"
    r"(?<![\w\u0590-\u05ff])(?:לכאורה|אולי|ייתכן|לא\s+מאומת|לא\s+ברור)"
    r"(?![\w\u0590-\u05ff])",
    re.IGNORECASE,
)
_PROHIBITED_GENERATED_CONTENT_RE = re.compile(
    r"(?:https?://|www\.)\S+|"
    r"\b(?:password|passcode|one[- ]time\s+(?:code|password)|login\s+details|"
    r"account\s+credentials?|authentication\s+code)\b|"
    r"\b(?:send|share|provide|enter|submit)\b.{0,80}\b"
    r"(?:credentials?|login|password|passcode|security\s+code)\b|"
    r"\b(?:visit|open|follow|navigate\s+to)\b.{0,100}\b"
    r"(?:external\s+(?:link|site|address|administrator)|provided\s+(?:link|address)|"
    r"security\s+verification|validate\s+your\s+account)\b|"
    r"\b(?:verify|validate)\b.{0,80}\b(?:account|identity|login|credentials?)\b",
    re.IGNORECASE,
)
_ENGLISH_SUBJECTLESS_ACCOMPLISHMENT_RE = re.compile(
    r"^(?:"
    r"administer(?:ed)?|analy[sz](?:e|ed)|architect(?:ed)?|author(?:ed)?|"
    r"automat(?:e|ed)|build|built|collaborat(?:e|ed)|configur(?:e|ed)|"
    r"complet(?:e|ed)|contribut(?:e|ed)|creat(?:e|ed)|defin(?:e|ed)|deliver(?:ed)?|"
    r"deploy(?:ed)?|design(?:ed)?|develop(?:ed)?|dr(?:ive|ove)|earn(?:ed)?|"
    r"engineer(?:ed)?|evaluat(?:e|ed)|implement(?:ed)?|improv(?:e|ed)|"
    r"integrat(?:e|ed)|investigat(?:e|ed)|"
    r"launch(?:ed)?|lead|led|maintain(?:ed)?|manag(?:e|ed)|migrat(?:e|ed)|"
    r"operat(?:e|ed)|optimiz(?:e|ed)|own(?:ed)?|program(?:med)?|provid(?:e|ed)|"
    r"reduc(?:e|ed)|review(?:ed)?|scal(?:e|ed)|stud(?:y|ied)|"
    r"support(?:ed)?|test(?:ed)?|"
    r"train(?:ed)?|use|used|work|worked|writ(?:e|ten)|wrote"
    r")\b",
    re.IGNORECASE,
)
_HEBREW_SUBJECTLESS_ACCOMPLISHMENT_RE = re.compile(
    r"^(?:"
    r"פיתחתי|בניתי|יצרתי|יישמתי|שיפרתי|עבדתי|למדתי|הובלתי|ניהלתי|"
    r"תכננתי|כתבתי|תחזקתי|הטמעתי|בדקתי|ניתחתי|פרסתי|הגדרתי|"
    r"מנוסה|מיומן|מיומנת|בקיא|בקיאה|זמין|זמינה"
    r")(?![\w\u0590-\u05ff])"
)
_LEADING_BULLET_RE = re.compile(r"^[-—–•*▪◦‣]+\s+")
_MAX_BOUND_CLAIMS = 50
_MAX_EVIDENCE_QUOTES_PER_CLAIM = 8


class EvidenceItemV1(BaseModel):
    """Private runtime evidence; source text is excluded from serialization."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(pattern=r"^ev_[0-9a-f]{24}$")
    source_kind: Literal["cv", "user_confirmed"]
    source_ref: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=800, exclude=True, repr=False)

    @model_validator(mode="after")
    def validate_content_address(self) -> EvidenceItemV1:
        expected = (
            "ev_"
            + _digest(
                f"{self.source_kind}:{self.source_ref}",
                self.text,
            )[:24]
        )
        if self.evidence_id != expected:
            raise ValueError("evidence_id does not match evidence content")
        return self


class ClaimEvidenceQuoteV1(BaseModel):
    """One literal evidence span cited by a generated factual claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(pattern=r"^ev_[0-9a-f]{24}$")
    quote: str = Field(
        min_length=1,
        max_length=800,
        exclude=True,
        repr=False,
        description=(
            "Exact character-for-character contiguous span copied from the cited "
            "evidence item; never a paraphrase or summary."
        ),
    )


class DraftClaimV1(BaseModel):
    """Claim contract emitted by the local typed material generator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(
        pattern=r"^claim_[a-zA-Z0-9_-]{1,80}$",
        exclude=True,
        repr=False,
        description=(
            "Non-semantic local ordinal such as claim_1. Never place candidate "
            "facts or evidence in this identifier."
        ),
    )
    claim_text: str = Field(
        min_length=1,
        max_length=1000,
        description="Exact factual sentence copied from the generated material.",
    )
    evidence_quotes: tuple[ClaimEvidenceQuoteV1, ...] = Field(
        min_length=1,
        max_length=8,
        description=(
            "Literal evidence spans supporting every independent factual clause in claim_text."
        ),
    )

    @model_validator(mode="after")
    def evidence_quotes_are_unique(self) -> DraftClaimV1:
        bindings = tuple((item.evidence_id, item.quote) for item in self.evidence_quotes)
        if len(bindings) != len(set(bindings)):
            raise ValueError("claim evidence quote bindings must be unique")
        return self

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        """Return ordered cited IDs without exposing private quote text."""

        return tuple(item.evidence_id for item in self.evidence_quotes)


class ClaimEvidenceRefV1(BaseModel):
    """Redacted, immutable validation result safe for material audit metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim_id: str = Field(pattern=r"^claim_[0-9a-f]{24}$")
    claim_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_ids: tuple[str, ...]
    evidence_quote_digests: tuple[str, ...]
    supported: bool
    reason_code: ClaimBlocker | None = None

    @model_validator(mode="after")
    def validate_support_state(self) -> ClaimEvidenceRefV1:
        if self.supported != (self.reason_code is None):
            raise ValueError("claim support state contradicts reason_code")
        if len(self.evidence_ids) != len(self.evidence_quote_digests):
            raise ValueError("each evidence ID requires one redacted quote digest")
        return self


class ClaimValidationV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: tuple[ClaimEvidenceRefV1, ...]
    blockers: tuple[ClaimBlocker, ...]

    @property
    def eligible(self) -> bool:
        return not self.blockers and all(claim.supported for claim in self.claims)


class ClaimEvaluationMetricsV1(BaseModel):
    """Aggregate-only offline metrics safe for reports and CI artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(ge=0)
    true_eligible: int = Field(ge=0)
    true_blocked: int = Field(ge=0)
    false_eligible: int = Field(ge=0)
    false_blocked: int = Field(ge=0)
    precision: float = Field(ge=0.0, le=1.0)
    recall: float = Field(ge=0.0, le=1.0)
    coverage: float = Field(ge=0.0, le=1.0)
    abstention_rate: float = Field(ge=0.0, le=1.0)

    @property
    def unsupported_eligible_count(self) -> int:
        return self.false_eligible


def _normalized(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip().casefold()


def _normalized_clause(value: str) -> str:
    normalized = _normalized(value)
    normalized = re.sub(r"^(?:and|but|while|whereas|plus)\s+", "", normalized)
    return normalized.strip(" \t\r\n,.;:!?")


def _digest(prefix: str, value: str) -> str:
    return hashlib.sha256(f"{prefix}\0{_normalized(value)}".encode()).hexdigest()


def make_evidence_item(
    source_kind: Literal["cv", "user_confirmed"],
    source_ref: str,
    text: str,
) -> EvidenceItemV1:
    """Create an evidence item whose ID is bound to source and content."""

    clean = _SPACE_RE.sub(" ", text).strip()
    if len(clean) > 800:
        raise ValueError("evidence item exceeds 800 characters")
    digest = _digest(f"{source_kind}:{source_ref}", clean)
    return EvidenceItemV1(
        evidence_id=f"ev_{digest[:24]}",
        source_kind=source_kind,
        source_ref=source_ref,
        text=clean,
    )


def _cv_chunks(text: str, *, max_chars: int = 600, limit: int = 24) -> Iterable[str]:
    """Create deterministic bounded evidence chunks without semantic inference."""

    physical_lines = [
        _SPACE_RE.sub(" ", raw).strip() for raw in re.split(r"[\r\n]+", text) if raw.strip()
    ]
    if not physical_lines:
        return

    # First classify each complete physical line. Then classify the exact
    # boundary context around adjacent lines so a PDF wrap cannot split a
    # protected phrase or instruction into individually safe fragments. A
    # complete unsafe line does not contaminate unrelated neighboring lines.
    unsafe_indexes = {
        index
        for index, line in enumerate(physical_lines)
        if contains_sensitive_text(line) or contains_prompt_injection(line)
    }
    for index in range(len(physical_lines) - 1):
        left = physical_lines[index]
        right = physical_lines[index + 1]
        first_token_match = re.match(r"^(?:[-*•▪◦‣]\s*)?(\S+)", right)
        if first_token_match is None:
            continue
        first_token = first_token_match.group(1)
        left_context = left[-240:]
        dehyphenated_left = re.sub(
            r"[\-\u058a\u05be\u2010-\u2015\u2e3a\u2e3b]\s*$",
            "",
            left_context,
        )
        left_is_unsafe = contains_sensitive_text(left_context) or contains_prompt_injection(
            left_context
        )
        token_is_unsafe = contains_sensitive_text(first_token) or contains_prompt_injection(
            first_token
        )
        boundary_candidates = (
            f"{left_context}\n{first_token}",
            f"{dehyphenated_left}{first_token}",
            f"{dehyphenated_left} {first_token}",
        )
        if (
            not left_is_unsafe
            and not token_is_unsafe
            and any(
                contains_sensitive_text(candidate) or contains_prompt_injection(candidate)
                for candidate in boundary_candidates
            )
        ):
            unsafe_indexes.update({index, index + 1})

    # A rare multi-line reconstruction that was not localized above abstains
    # only for its contiguous safe run, preserving unrelated CV evidence.
    run: list[int] = []
    for index in range(len(physical_lines) + 1):
        if index < len(physical_lines) and index not in unsafe_indexes:
            run.append(index)
            continue
        if run:
            candidate = "\n".join(physical_lines[item] for item in run)
            if contains_sensitive_text(candidate) or contains_prompt_injection(candidate):
                unsafe_indexes.update(run)
            run = []

    emitted = 0
    for line_index, physical_line in enumerate(physical_lines):
        if line_index in unsafe_indexes:
            continue
        for raw in re.split(r"(?<=[.!?])\s+", physical_line):
            line = _SPACE_RE.sub(" ", raw).strip()
            if not line:
                continue
            # Classify the complete source segment before fixed-size slicing so
            # a protected phrase cannot be split across two independently safe
            # chunks at the max_chars boundary.
            if contains_sensitive_text(line) or contains_prompt_injection(line):
                continue
            # Never turn part of a long source sentence into independently
            # affirmative evidence. A negation, attribution, or qualifier may
            # occur after the size boundary, so partial slicing would erase
            # material context. Long sentences abstain as a unit.
            if len(line) > max_chars:
                continue
            yield line
            emitted += 1
            if emitted >= limit:
                return


def non_sensitive_cv_excerpt(text: str, *, max_chars: int) -> str:
    """Return a bounded CV excerpt with sensitive/legal segments removed."""

    if max_chars < 1:
        return ""
    safe_segments: list[str] = []
    size = 0
    for segment in _cv_chunks(text):
        if contains_sensitive_text(segment) or contains_prompt_injection(segment):
            continue
        separator_size = 1 if safe_segments else 0
        required = separator_size + len(segment)
        if required > max_chars - size:
            continue
        safe_segments.append(segment)
        size += required
    return "\n".join(safe_segments)


def build_evidence_catalog(
    profile: UserProfile,
    cv_artifact: CVArtifact,
) -> tuple[EvidenceItemV1, ...]:
    """Build the only candidate evidence a material prompt may cite."""

    items: list[EvidenceItemV1] = []
    for key, value in sorted(profile.evidence.llm_safe_confirmed_facts().items()):
        # A second policy check is cheap and protects callers that construct a
        # ProfileEvidence object without going through normal validators. The
        # value check also prevents a sensitive fact hidden under an innocuous
        # or misspelled key from entering a material-generation prompt.
        if (
            not is_sensitive_fact_key(key)
            and not contains_sensitive_text(value)
            and not contains_prompt_injection(value)
        ):
            # Keep every catalog item independently auditable. A model emits
            # sentence-level prose, so a multi-sentence confirmed value is
            # represented by its complete literal sentences rather than by an
            # arbitrary substring of the combined value.
            items.extend(
                make_evidence_item("user_confirmed", key, sentence)
                for sentence in material_sentences((value,))
                if len(sentence) <= 800
            )
    items.extend(
        make_evidence_item("cv", cv_artifact.artifact_id, chunk)
        for chunk in _cv_chunks(cv_artifact.extracted_text)
    )
    deduplicated = {item.evidence_id: item for item in items}
    return tuple(deduplicated.values())


def render_evidence_catalog(catalog: Sequence[EvidenceItemV1]) -> str:
    """Render private evidence for one local prompt; never persist this value."""

    return "\n".join(f"[{item.evidence_id}] {item.text}" for item in catalog)


def _quote_supports_claim_clause(claim_clause: str, quote: str) -> bool:
    """Allow only literal evidence or a bounded grammatical subject wrapper.

    This deliberately does not attempt semantic entailment.  The source phrase
    after the wrapper must remain byte-for-byte equivalent after harmless
    whitespace, case, and terminal-punctuation normalization.  Consequently a
    model cannot add a title, metric, skill, employer, or qualitative modifier.
    """

    claim = _normalized_clause(claim_clause)
    source = _normalized_clause(quote)
    if not claim or not source:
        return False
    if claim == source:
        return True

    # Resume bullets commonly omit their subject.  These are the only
    # grammatical wrappers the renderer may add.  More expressive paraphrases
    # must stop for operator review because deterministic code cannot prove
    # their entailment.
    if claim == f"i {source}" and _ENGLISH_SUBJECTLESS_ACCOMPLISHMENT_RE.match(source):
        return True
    if claim == f"אני {source}" and _HEBREW_SUBJECTLESS_ACCOMPLISHMENT_RE.match(source):
        return True
    if claim == f"my {source}" and re.match(
        r"^(?:experience|background|expertise|availability|notice period)\b",
        source,
    ):
        return True
    if claim == f"i am {source}" and re.match(
        r"^(?:experienced|skilled|proficient|familiar|comfortable|available|based)\b",
        source,
    ):
        return True
    if claim == f"i have {source}" and re.match(
        r"^(?:\d|one\b|two\b|three\b|four\b|five\b|six\b|seven\b|eight\b|"
        r"nine\b|ten\b|developed\b|built\b|created\b|delivered\b|designed\b|"
        r"implemented\b|led\b|managed\b|worked\b|studied\b|experience\b)",
        source,
    ):
        return True
    return False


def _quote_is_exact_affirmative_span(quote: str, evidence_text: str) -> bool:
    """Require at least one literal occurrence in an affirmative source sentence."""

    for match in re.finditer(re.escape(quote), evidence_text):
        before = evidence_text[: match.start()]
        after = evidence_text[match.end() :]
        start = max(
            before.rfind("\n"),
            before.rfind("."),
            before.rfind("!"),
            before.rfind("?"),
        )
        boundaries = tuple(
            index for marker in ("\n", ".", "!", "?") if (index := after.find(marker)) >= 0
        )
        end = min(boundaries) if boundaries else len(after)
        sentence_start = start + 1
        sentence_end = match.end() + end
        context = evidence_text[sentence_start:sentence_end]
        if _NEGATED_EVIDENCE_CONTEXT_RE.search(context):
            continue
        if _UNCERTAIN_EVIDENCE_CONTEXT_RE.search(context):
            continue
        prefix = evidence_text[sentence_start : match.start()].strip()
        # A quote may start a resume sentence/bullet, follow a heading
        # marker, or omit only an explicit first-person grammatical subject.
        # It may not strip a heading/other person's subject or a limiting
        # phrase such as "observed engineers who" or "limited".
        if prefix and not (
            re.fullmatch(r"[-—–•*▪◦‣]+", prefix)
            or _normalized_clause(prefix)
            in {
                "i",
                "i have",
                "i am",
                "i've",
                "i'm",
                "my",
                "אני",
            }
        ):
            continue
        # The model may omit terminal punctuation, but it may not truncate an
        # attribution, qualifier, metric, denial, or any other factual suffix.
        if evidence_text[match.end() : sentence_end].strip():
            continue
        return True
    return False


def _requires_evidence(sentence: str) -> bool:
    clean = _SPACE_RE.sub(" ", sentence).strip()
    if (
        not clean
        or _SAFE_NONFACTUAL_FRAGMENT.fullmatch(clean)
        or _SAFE_SUBJECTIVE_FRAGMENT.fullmatch(clean)
        or _SAFE_HEBREW_SUBJECTIVE_FRAGMENT.fullmatch(clean)
    ):
        return False
    if _IMPLIED_CANDIDATE.search(sentence):
        return True
    first_person = bool(_FIRST_PERSON.search(sentence) or _HEBREW_FIRST_PERSON.search(sentence))
    factual = bool(
        _FACTUAL_MARKER.search(sentence)
        or _HEBREW_FACTUAL_MARKER.search(sentence)
        or _NUMBER_RE.search(sentence)
    )
    if first_person and factual:
        return True
    if _THIRD_PERSON_CANDIDATE.search(sentence):
        return True
    if _HEBREW_FACTUAL_MARKER.search(sentence):
        # Hebrew has no case distinction that can safely identify a candidate
        # name. Fail closed on factual Hebrew prose rather than treating it as
        # employer context and allowing an unsupported candidate assertion.
        return True
    if factual and not _EMPLOYER_CONTEXT.search(sentence):
        # Resume-style fragments, lowercase names, and candidate/applicant
        # prose are common model outputs. Treat every non-employer factual
        # assertion as candidate-specific unless independently declared.
        return True
    if not first_person:
        # Model output without an explicit subjective/salutation allowlist is
        # evidence-bearing. This includes long resume-style fragments and
        # employer/job assertions: neither may be invented from prompt prose.
        return True
    # Every remaining first-person assertion is evidence-bearing. Pure
    # expressions of interest were accepted only by the strict full-sentence
    # allowlists above.
    return True


def _factual_clauses(sentence: str) -> tuple[str, ...]:
    """Return independently auditable factual clauses from one sentence."""

    if not _requires_evidence(sentence):
        return ()
    parts = tuple(
        clause
        for raw_clause in _CLAUSE_RE.split(sentence)
        if (clause := _normalized_clause(raw_clause))
    )
    if len(parts) <= 1:
        normalized = _normalized_clause(sentence)
        return (normalized,) if normalized else ()

    sentence_has_candidate_subject = bool(
        _FIRST_PERSON.search(sentence)
        or _HEBREW_FIRST_PERSON.search(sentence)
        or _THIRD_PERSON_CANDIDATE.search(sentence)
        or _IMPLIED_CANDIDATE.search(sentence)
    )
    factual_parts = tuple(
        clause
        for raw_clause, clause in (
            (raw_clause, _normalized_clause(raw_clause))
            for raw_clause in _CLAUSE_RE.split(sentence)
        )
        if clause
        and (
            _requires_evidence(raw_clause)
            or (
                sentence_has_candidate_subject
                and (
                    _FACTUAL_MARKER.search(raw_clause)
                    or _HEBREW_FACTUAL_MARKER.search(raw_clause)
                    or _NUMBER_RE.search(raw_clause)
                )
            )
        )
    )
    return factual_parts or parts


def _declared_claim_clauses(claim: str) -> tuple[str, ...]:
    """Split every declared candidate claim into independently proven clauses."""

    clauses = tuple(
        clause
        for raw_clause in _CLAUSE_RE.split(claim)
        if (clause := _normalized_clause(raw_clause))
    )
    if clauses:
        return clauses
    normalized = _normalized_clause(claim)
    return (normalized,) if normalized else ()


def material_sentences(material_texts: Iterable[str]) -> list[str]:
    """Split material fields using the validator's canonical sentence boundary."""

    sentences: list[str] = []
    for text in material_texts:
        for sentence in _SENTENCE_RE.split(text):
            clean = _SPACE_RE.sub(" ", sentence).strip()
            if clean:
                sentences.append(clean)
    return sentences


def _evidence_quote_candidates(item: EvidenceItemV1) -> tuple[str, ...]:
    """Return only complete evidence text or that text with one bullet removed."""

    stripped_bullet = _LEADING_BULLET_RE.sub("", item.text, count=1)
    if stripped_bullet == item.text:
        return (item.text,)
    return (item.text, stripped_bullet)


def _literal_binding(
    claim_text: str,
    catalog: Sequence[EvidenceItemV1],
) -> ClaimEvidenceQuoteV1 | None:
    for item in catalog:
        for quote in _evidence_quote_candidates(item):
            if _quote_is_exact_affirmative_span(
                quote,
                item.text,
            ) and _quote_supports_claim_clause(claim_text, quote):
                return ClaimEvidenceQuoteV1(
                    evidence_id=item.evidence_id,
                    quote=quote,
                )
    return None


def bind_generated_claims(
    material_texts: Iterable[str],
    catalog: Sequence[EvidenceItemV1],
) -> tuple[DraftClaimV1, ...]:
    """Bind generated factual sentences to literal evidence deterministically.

    The model may draft prose, but it is not trusted to reproduce claim text,
    evidence IDs, or quotes consistently. This binder emits a claim only when
    every independently auditable clause in the exact generated sentence is
    supported by a complete affirmative catalog item under the same strict
    rules used by ``validate_claim_evidence``. Unsupported sentences remain
    undeclared and therefore block the package during validation.
    """

    claims: list[DraftClaimV1] = []
    seen_sentences: set[str] = set()
    for sentence in material_sentences(material_texts):
        normalized_sentence = _normalized(sentence)
        if normalized_sentence in seen_sentences:
            continue
        seen_sentences.add(normalized_sentence)
        clauses = _factual_clauses(sentence)
        if not clauses:
            continue

        # A complete evidence sentence may itself contain conjunctions. Match
        # it before clause splitting so one literal item stays one binding and
        # an exact sentence never exceeds the per-claim quote bound.
        whole_sentence_binding = _literal_binding(sentence, catalog)
        if whole_sentence_binding is not None:
            if len(claims) < _MAX_BOUND_CLAIMS:
                claims.append(
                    DraftClaimV1(
                        claim_id=f"claim_{len(claims) + 1}",
                        claim_text=sentence,
                        evidence_quotes=(whole_sentence_binding,),
                    )
                )
            continue

        bindings: list[ClaimEvidenceQuoteV1] = []
        supported = True
        for clause in clauses:
            match = _literal_binding(clause, catalog)
            if match is None:
                supported = False
                break
            if match not in bindings:
                bindings.append(match)
            if len(bindings) > _MAX_EVIDENCE_QUOTES_PER_CLAIM:
                supported = False
                break
        if supported and bindings and len(claims) < _MAX_BOUND_CLAIMS:
            claims.append(
                DraftClaimV1(
                    claim_id=f"claim_{len(claims) + 1}",
                    claim_text=sentence,
                    evidence_quotes=tuple(bindings),
                )
            )
    return tuple(claims)


def validate_claim_evidence(
    material_texts: Iterable[str],
    draft_claims: Sequence[DraftClaimV1],
    catalog: Sequence[EvidenceItemV1],
) -> ClaimValidationV1:
    """Validate declared claims and detect undeclared candidate assertions."""

    texts = tuple(material_texts)
    combined = _normalized("\n".join(texts))
    evidence_by_id = {item.evidence_id: item for item in catalog}
    results: list[ClaimEvidenceRefV1] = []
    blockers: set[ClaimBlocker] = set()
    supported_claim_clauses: set[str] = set()
    seen_claim_ids: set[str] = set()

    for claim in draft_claims:
        reason: ClaimBlocker | None = None
        normalized_claim = _normalized(claim.claim_text)
        if claim.claim_id in seen_claim_ids or normalized_claim not in combined:
            reason = "CLAIM_NOT_IN_MATERIAL"
        elif contains_sensitive_text(claim.claim_text):
            reason = "SENSITIVE_CLAIM_PROHIBITED"
        elif not claim.evidence_quotes:
            reason = "CLAIM_EVIDENCE_MISSING"
        elif any(evidence_id not in evidence_by_id for evidence_id in claim.evidence_ids):
            reason = "CLAIM_EVIDENCE_UNKNOWN"
        else:
            claim_clauses = _declared_claim_clauses(claim.claim_text)
            exact_bindings = tuple(
                binding
                for binding in claim.evidence_quotes
                if _quote_is_exact_affirmative_span(
                    binding.quote,
                    evidence_by_id[binding.evidence_id].text,
                )
                and not contains_sensitive_text(binding.quote)
                and not contains_prompt_injection(binding.quote)
            )
            whole_claim_supported = any(
                _quote_supports_claim_clause(claim.claim_text, binding.quote)
                for binding in exact_bindings
            )
            clauses_supported = bool(claim_clauses) and (
                whole_claim_supported
                or all(
                    any(
                        _quote_supports_claim_clause(clause, binding.quote)
                        for binding in exact_bindings
                    )
                    for clause in claim_clauses
                )
            )
            quotes_used = len(exact_bindings) == len(claim.evidence_quotes) and all(
                _quote_supports_claim_clause(claim.claim_text, binding.quote)
                or any(
                    _quote_supports_claim_clause(clause, binding.quote) for clause in claim_clauses
                )
                for binding in exact_bindings
            )
            if not clauses_supported or not quotes_used:
                reason = "CLAIM_EVIDENCE_MISMATCH"

        seen_claim_ids.add(claim.claim_id)
        supported = reason is None
        if reason is not None:
            blockers.add(reason)
        else:
            supported_claim_clauses.update(_declared_claim_clauses(claim.claim_text))
        results.append(
            ClaimEvidenceRefV1(
                claim_id=(
                    "claim_"
                    + _digest(
                        "claim-id",
                        f"{claim.claim_id}\0{claim.claim_text}",
                    )[:24]
                ),
                claim_digest=_digest("claim", claim.claim_text),
                evidence_ids=claim.evidence_ids,
                evidence_quote_digests=tuple(
                    _digest(f"quote:{binding.evidence_id}", binding.quote)
                    for binding in claim.evidence_quotes
                ),
                supported=supported,
                reason_code=reason,
            )
        )

    for sentence in material_sentences(texts):
        if contains_prompt_injection(sentence) or _PROHIBITED_GENERATED_CONTENT_RE.search(sentence):
            blockers.add("PROHIBITED_GENERATED_CONTENT")
        if contains_sensitive_text(sentence):
            blockers.add("SENSITIVE_CLAIM_PROHIBITED")
        for clause in _factual_clauses(sentence):
            if clause not in supported_claim_clauses:
                blockers.add("UNDECLARED_FACTUAL_CLAIM")

    return ClaimValidationV1(
        claims=tuple(results),
        blockers=tuple(sorted(blockers)),
    )


def evaluate_claim_dataset(
    rows: Sequence[Mapping[str, Any]],
) -> ClaimEvaluationMetricsV1:
    """Evaluate sanitized claim fixtures without returning source text or IDs."""

    if len(rows) > 1000:
        raise ValueError("claim evaluation dataset exceeds 1000 rows")
    true_eligible = true_blocked = false_eligible = false_blocked = 0

    for row_index, row in enumerate(rows):
        expected_eligible = row.get("expected_eligible")
        evidence_payload = row.get("evidence_catalog")
        segments = row.get("segments")
        if (
            not isinstance(expected_eligible, bool)
            or not isinstance(evidence_payload, Mapping)
            or not isinstance(segments, list)
        ):
            raise ValueError(f"invalid claim evaluation row at index {row_index}")

        catalog: list[EvidenceItemV1] = []
        evidence_lookup: dict[str, str] = {}
        for source_ref, raw_text in evidence_payload.items():
            if not isinstance(source_ref, str) or not isinstance(raw_text, str):
                raise ValueError(f"invalid evidence entry at row {row_index}")
            item = make_evidence_item(
                "cv" if source_ref.startswith("cv:") else "user_confirmed",
                source_ref,
                raw_text,
            )
            catalog.append(item)
            evidence_lookup[source_ref] = item.evidence_id

        material_texts: list[str] = []
        draft_claims: list[DraftClaimV1] = []
        for segment_index, segment in enumerate(segments):
            if not isinstance(segment, Mapping):
                raise ValueError(f"invalid material segment at row {row_index}")
            text = segment.get("text")
            claim_text = segment.get("claim_text")
            factual = segment.get("factual")
            declare_claim = segment.get("declare_claim")
            evidence_quotes = segment.get("evidence_quotes")
            if (
                not isinstance(text, str)
                or not isinstance(factual, bool)
                or not isinstance(declare_claim, bool)
                or not isinstance(evidence_quotes, list)
                or not all(isinstance(value, Mapping) for value in evidence_quotes)
                or (declare_claim and not isinstance(claim_text, str))
                or (not declare_claim and claim_text is not None)
            ):
                raise ValueError(f"invalid material segment at row {row_index}")
            material_texts.append(text)
            if factual and declare_claim and evidence_quotes:
                assert isinstance(claim_text, str)
                bindings: list[ClaimEvidenceQuoteV1] = []
                for evidence_quote in evidence_quotes:
                    source_ref = evidence_quote.get("evidence_ref")
                    quote = evidence_quote.get("quote")
                    if not isinstance(source_ref, str) or not isinstance(quote, str):
                        raise ValueError(f"invalid evidence quote at row {row_index}")
                    bindings.append(
                        ClaimEvidenceQuoteV1(
                            evidence_id=evidence_lookup.get(
                                source_ref,
                                "ev_"
                                + hashlib.sha256(
                                    f"unknown:{source_ref}".encode(),
                                ).hexdigest()[:24],
                            ),
                            quote=quote,
                        )
                    )
                draft_claims.append(
                    DraftClaimV1(
                        claim_id=f"claim_{row_index}_{segment_index}",
                        claim_text=claim_text,
                        evidence_quotes=tuple(bindings),
                    )
                )

        predicted_eligible = validate_claim_evidence(
            material_texts,
            draft_claims,
            catalog,
        ).eligible
        if predicted_eligible and expected_eligible:
            true_eligible += 1
        elif predicted_eligible:
            false_eligible += 1
        elif expected_eligible:
            false_blocked += 1
        else:
            true_blocked += 1

    predicted_eligible_count = true_eligible + false_eligible
    expected_eligible_count = true_eligible + false_blocked
    total = len(rows)
    precision = true_eligible / predicted_eligible_count if predicted_eligible_count else 0.0
    recall = true_eligible / expected_eligible_count if expected_eligible_count else 0.0
    coverage = predicted_eligible_count / total if total else 0.0
    return ClaimEvaluationMetricsV1(
        total=total,
        true_eligible=true_eligible,
        true_blocked=true_blocked,
        false_eligible=false_eligible,
        false_blocked=false_blocked,
        precision=precision,
        recall=recall,
        coverage=coverage,
        abstention_rate=1.0 - coverage if total else 0.0,
    )
