"""SQLAlchemy ORM models for the Job Apply Agent."""

from __future__ import annotations

import enum

from sqlalchemy import (
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


class URLStatus(str, enum.Enum):
    PENDING = "pending"
    FETCHED = "fetched"
    FAILED = "failed"
    BLOCKED = "blocked"  # bot protection / CAPTCHA


class JobStatus(str, enum.Enum):
    EXTRACTED = "extracted"
    SCORED = "scored"
    SKIPPED = "skipped"
    DRAFT = "draft"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    FAILED = "failed"


class SubmissionStatus(str, enum.Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    DRAFT_ONLY = "draft_only"
    NEEDS_HUMAN_CONFIRMATION = "needs_human_confirmation"


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

    __table_args__ = (
        Index("ix_messages_whatsapp_id", "whatsapp_message_id"),
    )


class ExtractedURL(Base):
    """URL extracted from a message."""

    __tablename__ = "extracted_urls"

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(Integer, ForeignKey("messages.id"), nullable=False)
    original_url = Column(Text, nullable=False)
    normalized_url = Column(Text, nullable=False)
    url_hash = Column(String(64), nullable=False)
    status = Column(Enum(URLStatus), default=URLStatus.PENDING, nullable=False)
    fetch_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    message = relationship("Message", back_populates="extracted_urls")
    jobs = relationship("Job", back_populates="extracted_url")

    __table_args__ = (
        Index("ix_extracted_urls_hash", "url_hash"),
    )


class Job(Base):
    """Extracted job posting."""

    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    extracted_url_id = Column(Integer, ForeignKey("extracted_urls.id"), nullable=False)
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
    status = Column(Enum(JobStatus), default=JobStatus.EXTRACTED, nullable=False)
    score = Column(Float, nullable=True)
    platform = Column(String(50), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    extracted_url = relationship("ExtractedURL", back_populates="jobs")
    application = relationship("Application", back_populates="job", uselist=False)

    __table_args__ = (
        Index("ix_jobs_apply_url_hash", "apply_url_hash"),
        Index("ix_jobs_signature", "job_signature"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_platform", "platform"),
    )


class Application(Base):
    """Generated application materials for a job."""

    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), unique=True, nullable=False)
    cover_letter = Column(Text, nullable=True)
    recruiter_message = Column(Text, nullable=True)
    qa_answers = Column(Text, nullable=True)  # JSON
    status = Column(Enum(JobStatus), default=JobStatus.DRAFT, nullable=False)
    approved_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now(), nullable=False)

    job = relationship("Job", back_populates="application")
    submission = relationship("Submission", back_populates="application", uselist=False)


class Submission(Base):
    """Record of an actual submission to a job board."""

    __tablename__ = "submissions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), unique=True, nullable=False)
    submitter_name = Column(String(100), nullable=False)  # e.g. "greenhouse", "lever"
    status = Column(Enum(SubmissionStatus), default=SubmissionStatus.PENDING, nullable=False)
    confirmation_url = Column(Text, nullable=True)
    confirmation_id = Column(String(255), nullable=True)
    error_message = Column(Text, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    application = relationship("Application", back_populates="submission")


class UserProfileVersion(Base):
    """Versioned snapshot of user profile for audit trail."""

    __tablename__ = "user_profile_versions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_yaml = Column(Text, nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=func.now(), nullable=False)
