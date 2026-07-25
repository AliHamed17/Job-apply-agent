"""SQLAlchemy ORM models for the Job Apply Agent."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    """Declarative base for all models."""

    pass


# ── Enums ────────────────────────────────────────────────


class URLStatus(str, enum.Enum):  # noqa: UP042 - preserve persisted enum behavior
    PENDING = "pending"
    FETCHED = "fetched"
    FAILED = "failed"
    BLOCKED = "blocked"  # bot protection / CAPTCHA


class JobStatus(str, enum.Enum):  # noqa: UP042 - preserve persisted enum behavior
    EXTRACTED = "extracted"
    SCORED = "scored"
    SKIPPED = "skipped"
    DRAFT = "draft"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class SubmissionStatus(str, enum.Enum):  # noqa: UP042 - preserve persisted enum behavior
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    DRAFT_ONLY = "draft_only"
    UNKNOWN = "unknown"


def _enum_values(enum_class) -> list[str]:
    """Persist enum values, matching the lowercase labels in Alembic."""
    return [member.value for member in enum_class]


# ── Models ───────────────────────────────────────────────


class Message(Base):
    """Incoming WhatsApp message."""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    whatsapp_message_id = Column(String(255), unique=True, nullable=False)
    sender_phone = Column(String(50), nullable=False)
    body = Column(Text, nullable=True)
    received_at = Column(DateTime, default=func.now(), nullable=False)
    correlation_id = Column(String(20), nullable=True)

    extracted_urls = relationship("ExtractedURL", back_populates="message")

    __table_args__ = (Index("ix_messages_whatsapp_id", "whatsapp_message_id"),)


class ExtractedURL(Base):
    """URL extracted from a message."""

    __tablename__ = "extracted_urls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False)
    original_url = Column(Text, nullable=False)
    normalized_url = Column(Text, nullable=False)
    url_hash = Column(String(64), nullable=False)
    status = Column(
        Enum(URLStatus, values_callable=_enum_values),
        default=URLStatus.PENDING,
        nullable=False,
    )
    fetch_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    message = relationship("Message", back_populates="extracted_urls")
    jobs = relationship("Job", back_populates="extracted_url")

    __table_args__ = (Index("ix_extracted_urls_hash", "url_hash"),)


class Job(Base):
    """Extracted job posting."""

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    extracted_url_id = Column(Integer, ForeignKey("extracted_urls.id"), nullable=True)
    title = Column(String(500), nullable=False)
    company = Column(String(300), nullable=True)
    location = Column(String(300), nullable=True)
    employment_type = Column(String(100), nullable=True)  # full-time, part-time, contract
    seniority = Column(String(100), nullable=True)
    description = Column(Text, nullable=True)
    requirements = Column(Text, nullable=True)
    apply_url = Column(Text, nullable=True)
    source_url = Column(Text, nullable=False)
    date_posted = Column(String(50), nullable=True)
    keywords = Column(Text, nullable=True)  # JSON-serialized list
    apply_url_hash = Column(String(64), nullable=True)
    job_signature = Column(String(64), nullable=True)  # hash(title+company+location)
    status = Column(
        Enum(JobStatus, values_callable=_enum_values),
        default=JobStatus.EXTRACTED,
        nullable=False,
    )
    score = Column(Float, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    discovery_source = Column(String(30), default="manual", nullable=True)
    easy_apply = Column(Boolean, default=False, nullable=True)
    expires_at = Column(DateTime, nullable=True)

    extracted_url = relationship("ExtractedURL", back_populates="jobs")
    application = relationship("Application", back_populates="job", uselist=False)

    __table_args__ = (
        Index("ix_jobs_apply_url_hash", "apply_url_hash"),
        Index("ix_jobs_signature", "job_signature"),
        Index("ix_jobs_status", "status"),
    )


class Application(Base):
    """Generated application materials for a job."""

    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), unique=True, nullable=False)
    cover_letter = Column(Text, nullable=True)
    recruiter_message = Column(Text, nullable=True)
    qa_answers = Column(Text, nullable=True)  # JSON
    status = Column(
        Enum(JobStatus, values_callable=_enum_values),
        default=JobStatus.DRAFT,
        nullable=False,
    )
    approved_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)
    submission_channel = Column(String(30), nullable=True)
    needs_review_reason = Column(Text, nullable=True)
    selected_cv_id = Column(String(255), nullable=True)
    profile_version = Column(Integer, nullable=True)
    cv_routing_confidence = Column(Float, nullable=True)
    cv_routing_evidence = Column(Text, nullable=True)
    cv_routing_fallback_reason = Column(String(64), nullable=True)
    cv_override_id = Column(String(255), nullable=True)
    outcome = Column(String(32), nullable=True)
    outcome_note = Column(Text, nullable=True)
    approval_source = Column(String(32), nullable=True)

    job = relationship("Job", back_populates="application")
    submissions = relationship(
        "Submission",
        back_populates="application",
        order_by="Submission.attempt_number",
        cascade="all, delete-orphan",
    )
    events = relationship(
        "ApplicationEvent",
        back_populates="application",
        order_by="ApplicationEvent.created_at",
        cascade="all, delete-orphan",
    )

    @property
    def submission(self):
        """Compatibility accessor for the latest submission attempt."""
        return self.submissions[-1] if self.submissions else None


class Submission(Base):
    """Record of an actual submission to a job board."""

    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    attempt_number = Column(Integer, nullable=False, default=1)
    idempotency_key = Column(
        String(36), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
    )
    submitter_name = Column(String(100), nullable=False)  # e.g. "greenhouse", "lever"
    status = Column(
        Enum(SubmissionStatus, values_callable=_enum_values),
        default=SubmissionStatus.PENDING,
        nullable=False,
    )
    confirmation_url = Column(Text, nullable=True)
    confirmation_id = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    reason_code = Column(String(64), nullable=True)
    diagnostic_details = Column(Text, nullable=True)
    selected_cv_id = Column(String(255), nullable=True)
    profile_version = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    reconciled_at = Column(DateTime, nullable=True)
    reconciliation_note = Column(Text, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    application = relationship("Application", back_populates="submissions")

    __table_args__ = (
        Index("ix_submissions_application_id", "application_id"),
        Index("ix_submissions_status", "status"),
        Index(
            "uq_submissions_application_attempt",
            "application_id",
            "attempt_number",
            unique=True,
        ),
    )


class ApplicationEvent(Base):
    """Durable, redacted audit event for an application lifecycle."""

    __tablename__ = "application_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    event_type = Column(String(64), nullable=False)
    actor = Column(String(32), nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    application = relationship("Application", back_populates="events")

    __table_args__ = (
        Index("ix_application_events_application_id", "application_id"),
        Index("ix_application_events_type", "event_type"),
    )


class UserProfileVersion(Base):
    """Versioned snapshot of user profile for audit trail."""

    __tablename__ = "user_profile_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_yaml = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=func.now(), nullable=False)


class BrowserQualificationRun(Base):
    """Privacy-safe record of a guarded browser smoke qualification."""

    __tablename__ = "browser_qualification_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    selector_version = Column(String(64), nullable=False)
    terminal_reason = Column(String(64), nullable=False)
    qualified = Column(Boolean, nullable=False, default=False)
    trace_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        Index(
            "ix_browser_qualification_selector_reason",
            "selector_version",
            "terminal_reason",
        ),
    )


class DiscoveryRun(Base):
    """Durable, privacy-safe status for one discovery provider run."""

    __tablename__ = "discovery_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False)
    inserted = Column(Integer, nullable=False, default=0)
    reason_code = Column(String(64), nullable=True)
    started_at = Column(DateTime, default=func.now(), nullable=False)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("ix_discovery_runs_source_finished", "source", "finished_at"),)


class CoverLetterFeedback(Base):
    """User corrections to LLM-generated cover letters (Phase 10 feedback loop).

    Stores original draft alongside the human-corrected version.
    These pairs are injected as few-shot examples into future LLM prompts,
    steering output toward the user's preferred style.
    """

    __tablename__ = "cover_letter_feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    original_text = Column(Text, nullable=False)  # LLM-generated draft
    corrected_text = Column(Text, nullable=False)  # Human-corrected version
    feedback_note = Column(Text, nullable=True)  # Optional explanation
    created_at = Column(DateTime, default=func.now(), nullable=False)

    application = relationship("Application", backref="feedbacks")

    __table_args__ = (Index("ix_cover_letter_feedback_app_id", "application_id"),)


class AnswerCache(Base):
    """Cached answers to recurring application-form questions."""

    __tablename__ = "answer_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question_hash = Column(String(64), unique=True, nullable=False)
    question_text = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    source = Column(String(20), nullable=False)  # deterministic | cache | llm
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (Index("ix_answer_cache_hash", "question_hash"),)


class OutboundContact(Base):
    """Dedup record for WhatsApp/email recruiter outreach."""

    __tablename__ = "outbound_contacts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contact_hash = Column(String(64), unique=True, nullable=False)
    channel = Column(String(20), nullable=False)  # whatsapp_dm | email
    last_contacted_at = Column(DateTime, default=func.now(), nullable=False)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (Index("ix_outbound_contact_hash", "contact_hash"),)
