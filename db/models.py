"""SQLAlchemy ORM models for the Job Apply Agent."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
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


def _submission_status_value(context) -> str:
    value = context.get_current_parameters().get("status")
    return value.value if isinstance(value, SubmissionStatus) else str(value or "")


def _default_attempt_stage(context) -> str:
    """Keep legacy ORM inserts consistent with the v4 stage/outcome checks."""
    if _submission_status_value(context) in {
        SubmissionStatus.SUCCESS.value,
        SubmissionStatus.FAILED.value,
        SubmissionStatus.DRAFT_ONLY.value,
        SubmissionStatus.UNKNOWN.value,
    }:
        return "finished"
    if _submission_status_value(context) == SubmissionStatus.RUNNING.value:
        return "committing"
    return "queued"


def _default_attempt_outcome(context) -> str | None:
    """Conservatively classify terminal legacy ORM inserts."""
    return {
        SubmissionStatus.SUCCESS.value: "legacy_unverified",
        SubmissionStatus.FAILED.value: "unknown",
        SubmissionStatus.DRAFT_ONLY.value: "draft_only",
        SubmissionStatus.UNKNOWN.value: "unknown",
    }.get(_submission_status_value(context))


def _sha256_check_sql(column_name: str) -> str:
    """Return a SQLite/PostgreSQL-compatible lowercase SHA-256 check."""
    remainder = column_name
    for character in "0123456789abcdef":
        remainder = f"replace({remainder}, '{character}', '')"
    return f"(length({column_name}) = 64 AND {remainder} = '')"


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
    selected_cv_hash = Column(String(64), nullable=True)
    profile_version = Column(Integer, nullable=True)
    cv_routing_confidence = Column(Float, nullable=True)
    cv_routing_evidence = Column(Text, nullable=True)
    cv_routing_fallback_reason = Column(String(64), nullable=True)
    cv_override_id = Column(String(255), nullable=True)
    outcome = Column(String(32), nullable=True)
    outcome_note = Column(Text, nullable=True)
    approval_source = Column(String(32), nullable=True)
    revision = Column(Integer, nullable=False, default=1, server_default=text("1"))
    prepared_revision = Column(Integer, nullable=True)
    material_eligible = Column(Boolean, nullable=True)
    material_blockers_json = Column(Text, nullable=False, default="[]", server_default="[]")
    material_claims_json = Column(Text, nullable=False, default="[]", server_default="[]")
    material_model_provider = Column(String(32), nullable=True)
    material_model_name = Column(String(128), nullable=True)
    material_model_digest = Column(String(71), nullable=True)
    material_prompt_version = Column(String(32), nullable=True)

    job = relationship("Job", back_populates="application")
    form_plans = relationship(
        "FormPlan",
        back_populates="application",
        order_by="(FormPlan.created_at, FormPlan.id)",
        cascade="all, delete-orphan",
    )
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
    control_plane_reference = relationship(
        "ControlPlaneApplicationRef",
        back_populates="application",
        cascade="all, delete-orphan",
        uselist=False,
    )
    control_plane_review_grants = relationship(
        "ControlPlaneReviewGrant",
        back_populates="application",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            f"selected_cv_hash IS NULL OR {_sha256_check_sql('selected_cv_hash')}",
            name="ck_applications_selected_cv_hash",
        ),
        CheckConstraint(
            "(material_prompt_version IS NULL AND material_model_provider IS NULL "
            "AND material_model_name IS NULL AND material_model_digest IS NULL) OR "
            "(material_prompt_version IS NOT NULL AND material_model_provider IS NOT NULL "
            "AND material_model_name IS NOT NULL AND material_model_digest IS NOT NULL)",
            name="ck_applications_material_identity_complete",
        ),
        CheckConstraint(
            "material_eligible IS NULL OR material_eligible = false OR "
            "(selected_cv_hash IS NOT NULL "
            "AND material_prompt_version IS NOT NULL "
            "AND material_model_provider IS NOT NULL "
            "AND material_model_name IS NOT NULL "
            "AND material_model_digest IS NOT NULL)",
            name="ck_applications_material_eligible_audited",
        ),
        CheckConstraint(
            "material_model_digest IS NULL OR "
            "(length(material_model_digest) = 71 "
            "AND substr(material_model_digest, 1, 7) = 'sha256:' "
            f"AND {_sha256_check_sql('substr(material_model_digest, 8)')})",
            name="ck_applications_material_model_digest",
        ),
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
        String(128), unique=True, nullable=False, default=lambda: str(uuid.uuid4())
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
    stage = Column(
        String(24),
        nullable=False,
        default=_default_attempt_stage,
        server_default="queued",
    )
    outcome = Column(String(32), nullable=True, default=_default_attempt_outcome)
    application_revision = Column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )
    adapter_name = Column(String(64), nullable=True)
    adapter_version = Column(String(32), nullable=True)
    selector_version = Column(String(64), nullable=True)
    form_plan_id = Column(Integer, nullable=True)
    form_plan_fingerprint = Column(String(64), nullable=True)
    requested_cv_id = Column(String(255), nullable=True)
    requested_cv_hash = Column(String(64), nullable=True)
    attached_cv_id = Column(String(255), nullable=True)
    attached_cv_hash = Column(String(64), nullable=True)
    attachment_verified = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    final_action_at = Column(DateTime, nullable=True)
    verification_kind = Column(String(64), nullable=True)
    evidence_digest = Column(String(64), nullable=True)
    runner_release = Column(String(64), nullable=True)
    legacy_reported_at = Column(DateTime, nullable=True)
    reconciliation_source = Column(String(32), nullable=True)
    reconciliation_evidence_ref = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    application = relationship("Application", back_populates="submissions")
    form_plan = relationship(
        "FormPlan",
        back_populates="submissions",
        primaryjoin="Submission.form_plan_id == FormPlan.id",
        foreign_keys=[form_plan_id],
    )
    final_submit_permit = relationship(
        "FinalSubmitPermit",
        back_populates="attempt",
        cascade="all, delete-orphan",
        uselist=False,
    )
    command = relationship(
        "SubmissionCommand",
        back_populates="attempt",
        cascade="all, delete-orphan",
        uselist=False,
    )
    evidence = relationship(
        "SubmissionEvidence",
        back_populates="attempt",
        order_by="SubmissionEvidence.observed_at",
        cascade="all, delete-orphan",
        foreign_keys="SubmissionEvidence.attempt_id",
    )

    __table_args__ = (
        CheckConstraint(
            "stage IN ('queued', 'inspecting', 'preparing', 'ready', "
            "'committing', 'verifying', 'finished')",
            name="ck_submissions_attempt_stage",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN "
            "('confirmed_submitted', 'already_applied', 'needs_review', "
            "'unknown', 'failed_before_commit', 'draft_only', "
            "'operator_confirmed', 'legacy_unverified')",
            name="ck_submissions_attempt_outcome",
        ),
        CheckConstraint(
            "(stage = 'finished' AND outcome IS NOT NULL) OR "
            "(stage <> 'finished' AND outcome IS NULL)",
            name="ck_submissions_stage_outcome_consistent",
        ),
        CheckConstraint(
            "status <> 'success' OR "
            "(stage = 'finished' AND "
            "outcome IN ('confirmed_submitted', 'legacy_unverified'))",
            name="ck_submissions_success_outcome",
        ),
        CheckConstraint(
            "submitted_at IS NULL OR "
            "(stage = 'finished' AND outcome = 'confirmed_submitted' "
            "AND status = 'success')",
            name="ck_submissions_submitted_at_verified",
        ),
        CheckConstraint(
            "outcome <> 'confirmed_submitted' OR "
            "(status = 'success' AND submitted_at IS NOT NULL "
            "AND final_action_at IS NOT NULL "
            "AND submitted_at >= final_action_at "
            "AND form_plan_id IS NOT NULL "
            "AND adapter_name IS NOT NULL "
            "AND length(trim(adapter_name)) > 0 "
            "AND adapter_version IS NOT NULL "
            "AND length(trim(adapter_version)) > 0 "
            "AND selector_version IS NOT NULL "
            "AND length(trim(selector_version)) > 0 "
            "AND profile_version IS NOT NULL "
            "AND profile_version > 0 "
            "AND runner_release IS NOT NULL "
            "AND length(trim(runner_release)) > 0 "
            "AND length(runner_release) <= 64 "
            "AND requested_cv_id IS NOT NULL "
            "AND length(trim(requested_cv_id)) > 0 "
            "AND attached_cv_id IS NOT NULL "
            "AND length(trim(attached_cv_id)) > 0 "
            "AND requested_cv_id = attached_cv_id "
            "AND requested_cv_hash IS NOT NULL "
            "AND attachment_verified = true "
            "AND attached_cv_hash IS NOT NULL "
            "AND requested_cv_hash = attached_cv_hash "
            f"AND {_sha256_check_sql('attached_cv_hash')} "
            "AND form_plan_fingerprint IS NOT NULL "
            f"AND {_sha256_check_sql('form_plan_fingerprint')} "
            "AND verification_kind IS NOT NULL "
            "AND verification_kind IN "
            "('employer_application_id', 'api_receipt', "
            "'candidate_portal_record', 'visible_post_click_confirmation') "
            "AND evidence_digest IS NOT NULL "
            f"AND {_sha256_check_sql('evidence_digest')})",
            name="ck_submissions_confirmed_evidence",
        ),
        ForeignKeyConstraint(
            [
                "form_plan_id",
                "application_id",
                "application_revision",
                "adapter_name",
                "adapter_version",
                "selector_version",
                "form_plan_fingerprint",
                "requested_cv_id",
                "requested_cv_hash",
                "attached_cv_id",
                "attached_cv_hash",
                "attachment_verified",
                "profile_version",
            ],
            [
                "form_plans.id",
                "form_plans.application_id",
                "form_plans.application_revision",
                "form_plans.adapter_name",
                "form_plans.adapter_version",
                "form_plans.selector_version",
                "form_plans.fingerprint",
                "form_plans.selected_cv_id",
                "form_plans.selected_cv_hash",
                "form_plans.attached_cv_id",
                "form_plans.attached_cv_hash",
                "form_plans.attachment_verified",
                "form_plans.profile_version",
            ],
            name="fk_submissions_exact_form_plan",
        ),
        ForeignKeyConstraint(
            [
                "id",
                "evidence_digest",
                "verification_kind",
                "form_plan_fingerprint",
                "attached_cv_hash",
            ],
            [
                "submission_evidence.attempt_id",
                "submission_evidence.evidence_digest",
                "submission_evidence.evidence_type",
                "submission_evidence.form_fingerprint",
                "submission_evidence.cv_hash",
            ],
            name="fk_submissions_confirmed_evidence",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index("ix_submissions_application_id", "application_id"),
        Index("ix_submissions_status", "status"),
        Index("ix_submissions_stage", "stage"),
        Index("ix_submissions_outcome", "outcome"),
        UniqueConstraint(
            "application_id",
            "attempt_number",
            name="uq_submissions_application_attempt",
        ),
        Index(
            "uq_submissions_one_unfinished_per_application",
            "application_id",
            unique=True,
            postgresql_where=text("stage <> 'finished'"),
            sqlite_where=text("stage <> 'finished'"),
        ),
    )


class FormPlan(Base):
    """Immutable, expiring snapshot of an observed application form."""

    __tablename__ = "form_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_id = Column(
        String(36),
        nullable=False,
        default=lambda: str(uuid.uuid4()),
    )
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    application_revision = Column(Integer, nullable=False)
    adapter_name = Column(String(64), nullable=False)
    adapter_version = Column(String(32), nullable=False)
    selector_version = Column(String(64), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    selected_cv_id = Column(String(255), nullable=False)
    selected_cv_hash = Column(String(64), nullable=False)
    attached_cv_id = Column(String(255), nullable=True)
    attached_cv_hash = Column(String(64), nullable=True)
    attachment_verified = Column(
        Boolean,
        nullable=False,
        default=False,
        server_default=false(),
    )
    attachment_verification_source = Column(String(64), nullable=True)
    attachment_verified_at = Column(DateTime, nullable=True)
    profile_version = Column(Integer, nullable=True)
    fields_json = Column(Text, nullable=False, default="[]", server_default="[]")
    disclosures_json = Column(Text, nullable=False, default="[]", server_default="[]")
    decisions_json = Column(Text, nullable=False, default="[]", server_default="[]")
    blockers_json = Column(Text, nullable=False, default="[]", server_default="[]")
    locale = Column(String(32), nullable=False, default="en", server_default="en")
    answer_policy_version = Column(
        String(64),
        nullable=False,
        default="answer-policy-v1",
        server_default="answer-policy-v1",
    )
    llm_prompt_version = Column(String(32), nullable=True)
    llm_model_provider = Column(String(32), nullable=True)
    llm_model_name = Column(String(128), nullable=True)
    llm_model_digest = Column(String(71), nullable=True)
    session_verified_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    expires_at = Column(DateTime, nullable=False)
    invalidated_at = Column(DateTime, nullable=True)
    invalidation_reason = Column(String(64), nullable=True)

    application = relationship("Application", back_populates="form_plans")
    submissions = relationship(
        "Submission",
        back_populates="form_plan",
        primaryjoin="Submission.form_plan_id == FormPlan.id",
        foreign_keys="Submission.form_plan_id",
    )

    __table_args__ = (
        UniqueConstraint("plan_id", name="uq_form_plans_plan_id"),
        UniqueConstraint(
            "id",
            "application_id",
            "application_revision",
            "adapter_name",
            "adapter_version",
            "selector_version",
            "fingerprint",
            "selected_cv_id",
            "selected_cv_hash",
            "attached_cv_id",
            "attached_cv_hash",
            "attachment_verified",
            "profile_version",
            name="uq_form_plans_submission_binding",
        ),
        Index(
            "ix_form_plans_application_revision",
            "application_id",
            "application_revision",
        ),
        Index("ix_form_plans_expires_at", "expires_at"),
        Index("ix_form_plans_fingerprint", "fingerprint"),
        CheckConstraint(
            "(llm_prompt_version IS NULL AND llm_model_provider IS NULL "
            "AND llm_model_name IS NULL AND llm_model_digest IS NULL) OR "
            "(llm_prompt_version IS NOT NULL AND llm_model_provider IS NOT NULL "
            "AND llm_model_name IS NOT NULL AND llm_model_digest IS NOT NULL)",
            name="ck_form_plans_llm_identity_complete",
        ),
        CheckConstraint(
            "length(trim(locale)) > 0 AND length(trim(answer_policy_version)) > 0",
            name="ck_form_plans_policy_metadata",
        ),
        CheckConstraint(
            "(attachment_verification_source IS NULL "
            "AND attachment_verified_at IS NULL) OR "
            "(attachment_verification_source IS NOT NULL "
            "AND length(trim(attachment_verification_source)) BETWEEN 1 AND 64 "
            "AND attachment_verified_at IS NOT NULL)",
            name="ck_form_plans_attachment_evidence_metadata",
        ),
        CheckConstraint(
            "llm_model_digest IS NULL OR "
            "(length(llm_model_digest) = 71 "
            "AND substr(llm_model_digest, 1, 7) = 'sha256:' "
            f"AND {_sha256_check_sql('substr(llm_model_digest, 8)')})",
            name="ck_form_plans_llm_model_digest",
        ),
    )


class FinalSubmitPermit(Base):
    """One-use, hash-bound authority for an irreversible final action."""

    __tablename__ = "final_submit_permits"

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(
        Integer,
        ForeignKey("submissions.id"),
        nullable=False,
    )
    nonce_hash = Column(String(64), nullable=False)
    job_url_hash = Column(String(64), nullable=False)
    application_revision = Column(Integer, nullable=False)
    adapter_name = Column(String(64), nullable=False)
    adapter_version = Column(String(32), nullable=False)
    selector_version = Column(String(64), nullable=False)
    form_plan_fingerprint = Column(String(64), nullable=False)
    cv_hash = Column(String(64), nullable=False)
    issued_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime, nullable=True)

    attempt = relationship("Submission", back_populates="final_submit_permit")

    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_final_submit_permits_attempt_id"),
        UniqueConstraint("nonce_hash", name="uq_final_submit_permits_nonce_hash"),
        Index("ix_final_submit_permits_expires_at", "expires_at"),
    )


class SubmissionCommand(Base):
    """Authoritative database outbox command for one exact attempt."""

    __tablename__ = "submission_commands"

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(
        Integer,
        ForeignKey("submissions.id"),
        nullable=False,
    )
    idempotency_key = Column(String(128), nullable=False)
    state = Column(String(16), nullable=False, default="pending", server_default="pending")
    available_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    claimed_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    claimed_by = Column(String(128), nullable=True)
    claim_token = Column(String(64), nullable=True)
    last_error_code = Column(String(64), nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )

    attempt = relationship("Submission", back_populates="command")

    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'claimed', 'completed', 'cancelled')",
            name="ck_submission_commands_state",
        ),
        CheckConstraint(
            "(state = 'pending' AND claimed_at IS NULL "
            "AND claimed_by IS NULL AND claim_token IS NULL "
            "AND completed_at IS NULL) "
            "OR (state = 'claimed' AND claimed_at IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claim_token IS NOT NULL "
            "AND completed_at IS NULL) "
            "OR (state IN ('completed', 'cancelled') "
            "AND completed_at IS NOT NULL AND claimed_at IS NULL "
            "AND claimed_by IS NULL AND claim_token IS NULL)",
            name="ck_submission_commands_state_metadata",
        ),
        UniqueConstraint("attempt_id", name="uq_submission_commands_attempt_id"),
        UniqueConstraint(
            "idempotency_key",
            name="uq_submission_commands_idempotency_key",
        ),
        Index("ix_submission_commands_state_available", "state", "available_at"),
    )


class ControlPlaneApplicationRef(Base):
    """Opaque public reference for one private local application."""

    __tablename__ = "control_plane_application_refs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
        nullable=False,
    )
    remote_ref = Column(String(64), nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    last_projected_at = Column(DateTime, nullable=True)

    application = relationship("Application", back_populates="control_plane_reference")
    review_grants = relationship(
        "ControlPlaneReviewGrant",
        back_populates="application_ref",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "length(remote_ref) BETWEEN 32 AND 64",
            name="ck_control_plane_application_refs_bounded",
        ),
        UniqueConstraint(
            "application_id",
            name="uq_control_plane_application_refs_application_id",
        ),
        UniqueConstraint(
            "remote_ref",
            name="uq_control_plane_application_refs_remote_ref",
        ),
    )


class ControlPlaneReviewGrant(Base):
    """Private binding behind a short-lived opaque remote review grant."""

    __tablename__ = "control_plane_review_grants"

    id = Column(Integer, primary_key=True, autoincrement=True)
    grant_ref = Column(String(64), nullable=False)
    application_id = Column(
        Integer,
        ForeignKey("applications.id"),
        nullable=False,
    )
    application_ref_id = Column(
        Integer,
        ForeignKey("control_plane_application_refs.id"),
        nullable=False,
    )
    form_plan_id = Column(Integer, ForeignKey("form_plans.id"), nullable=False)
    application_revision = Column(Integer, nullable=False)
    job_url_hash = Column(String(64), nullable=False)
    form_plan_fingerprint = Column(String(64), nullable=False)
    cv_hash = Column(String(64), nullable=False)
    adapter_name = Column(String(64), nullable=False)
    adapter_version = Column(String(32), nullable=False)
    selector_version = Column(String(64), nullable=False)
    runner_release = Column(String(64), nullable=False)
    issued_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    consumed_at = Column(DateTime, nullable=True)
    consumed_command_ref = Column(String(64), nullable=True)
    projection_state = Column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    projection_available_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    projection_claimed_at = Column(DateTime, nullable=True)
    projection_claimed_by = Column(String(64), nullable=True)
    projection_claim_token = Column(String(64), nullable=True)
    projection_attempts = Column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    projected_at = Column(DateTime, nullable=True)
    last_projection_error_code = Column(String(64), nullable=True)

    application = relationship("Application", back_populates="control_plane_review_grants")
    application_ref = relationship(
        "ControlPlaneApplicationRef",
        back_populates="review_grants",
    )
    form_plan = relationship("FormPlan")
    receipt = relationship(
        "ControlPlaneCommandReceipt",
        back_populates="review_grant",
        cascade="all, delete-orphan",
        uselist=False,
    )

    __table_args__ = (
        CheckConstraint(
            "length(grant_ref) BETWEEN 32 AND 64 "
            "AND application_revision > 0 "
            "AND length(trim(adapter_name)) BETWEEN 1 AND 64 "
            "AND length(trim(adapter_version)) BETWEEN 1 AND 32 "
            "AND length(trim(selector_version)) BETWEEN 1 AND 64 "
            "AND length(trim(runner_release)) BETWEEN 1 AND 64",
            name="ck_control_plane_review_grants_metadata",
        ),
        CheckConstraint(
            f"{_sha256_check_sql('job_url_hash')} "
            f"AND {_sha256_check_sql('form_plan_fingerprint')} "
            f"AND {_sha256_check_sql('cv_hash')}",
            name="ck_control_plane_review_grants_digests",
        ),
        CheckConstraint(
            "expires_at > issued_at",
            name="ck_control_plane_review_grants_lifetime",
        ),
        CheckConstraint(
            "(consumed_at IS NULL AND consumed_command_ref IS NULL) OR "
            "(consumed_at IS NOT NULL AND consumed_command_ref IS NOT NULL "
            "AND length(trim(consumed_command_ref)) BETWEEN 1 AND 64)",
            name="ck_control_plane_review_grants_consumption",
        ),
        CheckConstraint(
            "projection_state IN ('pending', 'claimed', 'projected') AND projection_attempts >= 0",
            name="ck_control_plane_review_grants_projection_state",
        ),
        CheckConstraint(
            "(projection_state = 'pending' "
            "AND projection_claimed_at IS NULL "
            "AND projection_claimed_by IS NULL "
            "AND projection_claim_token IS NULL "
            "AND projected_at IS NULL) "
            "OR (projection_state = 'claimed' "
            "AND projection_claimed_at IS NOT NULL "
            "AND projection_claimed_by IS NOT NULL "
            "AND projection_claim_token IS NOT NULL "
            "AND projected_at IS NULL) "
            "OR (projection_state = 'projected' "
            "AND projection_claimed_at IS NULL "
            "AND projection_claimed_by IS NULL "
            "AND projection_claim_token IS NULL "
            "AND projected_at IS NOT NULL)",
            name="ck_control_plane_review_grants_projection_metadata",
        ),
        UniqueConstraint(
            "grant_ref",
            name="uq_control_plane_review_grants_grant_ref",
        ),
        Index(
            "ix_control_plane_review_grants_application",
            "application_id",
            "issued_at",
        ),
        Index(
            "ix_control_plane_review_grants_expiry",
            "expires_at",
        ),
        Index(
            "ix_control_plane_review_grants_projection",
            "projection_state",
            "projection_available_at",
        ),
    )


class ControlPlaneCommandReceipt(Base):
    """One durable receipt for one authenticated remote command delivery."""

    __tablename__ = "control_plane_command_receipts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    remote_command_ref = Column(String(64), nullable=False)
    remote_attempt_ref = Column(String(64), nullable=False)
    review_grant_id = Column(
        Integer,
        ForeignKey("control_plane_review_grants.id"),
        nullable=False,
    )
    delivery_nonce_hash = Column(String(64), nullable=False)
    envelope_digest = Column(String(64), nullable=False)
    client_idempotency_key = Column(String(128), nullable=False)
    accepted_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    review_grant = relationship("ControlPlaneReviewGrant", back_populates="receipt")

    __table_args__ = (
        CheckConstraint(
            "length(remote_command_ref) BETWEEN 16 AND 64 "
            "AND length(remote_attempt_ref) BETWEEN 32 AND 64 "
            "AND length(client_idempotency_key) BETWEEN 16 AND 128",
            name="ck_control_plane_command_receipts_metadata",
        ),
        CheckConstraint(
            f"{_sha256_check_sql('delivery_nonce_hash')} "
            f"AND {_sha256_check_sql('envelope_digest')}",
            name="ck_control_plane_command_receipts_digests",
        ),
        UniqueConstraint(
            "remote_command_ref",
            name="uq_control_plane_command_receipts_command_ref",
        ),
        UniqueConstraint(
            "remote_attempt_ref",
            name="uq_control_plane_command_receipts_attempt_ref",
        ),
        UniqueConstraint(
            "review_grant_id",
            name="uq_control_plane_command_receipts_review_grant",
        ),
        UniqueConstraint(
            "delivery_nonce_hash",
            name="uq_control_plane_command_receipts_delivery_nonce",
        ),
        UniqueConstraint(
            "client_idempotency_key",
            name="uq_control_plane_command_receipts_idempotency",
        ),
    )


class ControlPlaneEventOutbox(Base):
    """Durable redacted event waiting for signed outbound delivery."""

    __tablename__ = "control_plane_event_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_ref = Column(String(64), nullable=False)
    remote_command_ref = Column(String(64), nullable=False)
    sequence = Column(Integer, nullable=False)
    cycle = Column(Integer, nullable=False, default=0, server_default=text("0"))
    event_type = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    payload_digest = Column(String(64), nullable=False)
    state = Column(String(16), nullable=False, default="pending", server_default="pending")
    available_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    claimed_at = Column(DateTime, nullable=True)
    claimed_by = Column(String(64), nullable=True)
    claim_token = Column(String(64), nullable=True)
    sent_at = Column(DateTime, nullable=True)
    delivery_count = Column(Integer, nullable=False, default=0, server_default=text("0"))
    last_error_code = Column(String(64), nullable=True)
    created_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "length(event_ref) BETWEEN 32 AND 64 "
            "AND length(remote_command_ref) BETWEEN 16 AND 64 "
            "AND sequence > 0 AND cycle >= 0 "
            "AND length(trim(event_type)) BETWEEN 1 AND 64 "
            "AND length(payload_json) BETWEEN 2 AND 4096 "
            "AND delivery_count >= 0",
            name="ck_control_plane_event_outbox_metadata",
        ),
        CheckConstraint(
            _sha256_check_sql("payload_digest"),
            name="ck_control_plane_event_outbox_digest",
        ),
        CheckConstraint(
            "state IN ('pending', 'claimed', 'sent')",
            name="ck_control_plane_event_outbox_state",
        ),
        CheckConstraint(
            "(state = 'pending' AND claimed_at IS NULL "
            "AND claimed_by IS NULL AND claim_token IS NULL AND sent_at IS NULL) "
            "OR (state = 'claimed' AND claimed_at IS NOT NULL "
            "AND claimed_by IS NOT NULL AND claim_token IS NOT NULL "
            "AND sent_at IS NULL) "
            "OR (state = 'sent' AND claimed_at IS NULL "
            "AND claimed_by IS NULL AND claim_token IS NULL "
            "AND sent_at IS NOT NULL)",
            name="ck_control_plane_event_outbox_state_metadata",
        ),
        UniqueConstraint(
            "event_ref",
            name="uq_control_plane_event_outbox_event_ref",
        ),
        UniqueConstraint(
            "remote_command_ref",
            "sequence",
            name="uq_control_plane_event_outbox_command_sequence",
        ),
        Index(
            "ix_control_plane_event_outbox_state_available",
            "state",
            "available_at",
        ),
    )


class SubmissionEvidence(Base):
    """Redacted employer evidence tied to one exact submission attempt."""

    __tablename__ = "submission_evidence"

    id = Column(Integer, primary_key=True, autoincrement=True)
    attempt_id = Column(Integer, ForeignKey("submissions.id"), nullable=False)
    evidence_type = Column(String(64), nullable=False)
    evidence_digest = Column(String(64), nullable=False)
    employer_application_ref = Column(String(255), nullable=True)
    receipt_ref = Column(String(255), nullable=True)
    portal_record_ref = Column(String(255), nullable=True)
    form_fingerprint = Column(String(64), nullable=False)
    cv_hash = Column(String(64), nullable=False)
    observed_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    attempt = relationship(
        "Submission",
        back_populates="evidence",
        foreign_keys=[attempt_id],
    )

    __table_args__ = (
        CheckConstraint(
            "evidence_type IN "
            "('employer_application_id', 'api_receipt', "
            "'candidate_portal_record', 'visible_post_click_confirmation')",
            name="ck_submission_evidence_type",
        ),
        CheckConstraint(
            f"{_sha256_check_sql('evidence_digest')} "
            f"AND {_sha256_check_sql('form_fingerprint')} "
            f"AND {_sha256_check_sql('cv_hash')}",
            name="ck_submission_evidence_digests",
        ),
        CheckConstraint(
            "(evidence_type = 'employer_application_id' "
            "AND employer_application_ref IS NOT NULL "
            "AND length(trim(employer_application_ref)) > 0 "
            "AND receipt_ref IS NULL AND portal_record_ref IS NULL) "
            "OR (evidence_type = 'api_receipt' "
            "AND receipt_ref IS NOT NULL "
            "AND length(trim(receipt_ref)) > 0 "
            "AND employer_application_ref IS NULL AND portal_record_ref IS NULL) "
            "OR (evidence_type = 'candidate_portal_record' "
            "AND portal_record_ref IS NOT NULL "
            "AND length(trim(portal_record_ref)) > 0 "
            "AND employer_application_ref IS NULL AND receipt_ref IS NULL) "
            "OR (evidence_type = 'visible_post_click_confirmation' "
            "AND employer_application_ref IS NULL "
            "AND receipt_ref IS NULL AND portal_record_ref IS NULL)",
            name="ck_submission_evidence_typed_reference",
        ),
        UniqueConstraint(
            "attempt_id",
            "evidence_digest",
            name="uq_submission_evidence_attempt_digest",
        ),
        UniqueConstraint(
            "attempt_id",
            "evidence_digest",
            "evidence_type",
            "form_fingerprint",
            "cv_hash",
            name="uq_submission_evidence_binding",
        ),
        Index("ix_submission_evidence_attempt_id", "attempt_id"),
        Index("ix_submission_evidence_type", "evidence_type"),
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

    __table_args__ = (
        UniqueConstraint(
            "version",
            name="uq_user_profile_versions_version",
        ),
    )


class BrowserQualificationRun(Base):
    """Privacy-safe record of a guarded browser smoke qualification."""

    __tablename__ = "browser_qualification_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    selector_version = Column(String(64), nullable=False)
    terminal_reason = Column(String(64), nullable=False)
    qualified = Column(Boolean, nullable=False, default=False)
    trace_json = Column(Text, nullable=False)
    adapter_name = Column(String(64), nullable=True)
    adapter_version = Column(String(32), nullable=True)
    qualification_tier = Column(String(32), nullable=True)
    form_fingerprint = Column(String(64), nullable=True)
    fixture_digest = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=func.now(), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "qualification_tier IS NULL OR qualification_tier IN "
            "('disabled', 'dry_run_only', 'fixture_qualified', "
            "'dry_run_qualified', 'live_canary_qualified')",
            name="ck_browser_qualification_tier",
        ),
        CheckConstraint(
            "(adapter_name IS NULL AND adapter_version IS NULL "
            "AND qualification_tier IS NULL AND form_fingerprint IS NULL "
            "AND fixture_digest IS NULL) OR "
            "(adapter_name IS NOT NULL AND adapter_version IS NOT NULL "
            "AND qualification_tier IS NOT NULL AND fixture_digest IS NOT NULL "
            "AND length(trim(adapter_name)) BETWEEN 1 AND 64 "
            "AND length(trim(adapter_version)) BETWEEN 1 AND 32)",
            name="ck_browser_qualification_metadata_complete",
        ),
        CheckConstraint(
            f"(fixture_digest IS NULL OR {_sha256_check_sql('fixture_digest')}) "
            f"AND (form_fingerprint IS NULL OR "
            f"{_sha256_check_sql('form_fingerprint')})",
            name="ck_browser_qualification_digests",
        ),
        CheckConstraint(
            "qualification_tier IS NULL "
            "OR qualification_tier != 'live_canary_qualified' "
            "OR (qualified = true "
            "AND terminal_reason = 'LIVE_CANARY_CONFIRMED' "
            "AND form_fingerprint IS NOT NULL)",
            name="ck_browser_qualification_live_evidence",
        ),
        Index(
            "ix_browser_qualification_selector_reason",
            "selector_version",
            "terminal_reason",
        ),
        Index(
            "ix_browser_qualification_adapter_tier",
            "adapter_name",
            "adapter_version",
            "qualification_tier",
            "created_at",
        ),
        Index(
            "ix_browser_qualification_adapter_form",
            "adapter_name",
            "adapter_version",
            "form_fingerprint",
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


class OperatorApprovedAnswer(Base):
    """Explicit reusable answer scoped to one exact form and policy context."""

    __tablename__ = "operator_approved_answers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    canonical_field = Column(String(255), nullable=False)
    field_type = Column(String(32), nullable=False)
    option_set_hash = Column(String(64), nullable=False)
    locale = Column(String(32), nullable=False)
    profile_version = Column(Integer, nullable=False)
    selected_cv_id = Column(String(255), nullable=False)
    selected_cv_hash = Column(String(64), nullable=False)
    adapter_name = Column(String(64), nullable=False)
    adapter_version = Column(String(32), nullable=False)
    selector_version = Column(String(64), nullable=False)
    form_fingerprint = Column(String(64), nullable=False)
    field_contract_fingerprint = Column(String(64), nullable=True)
    policy_version = Column(String(64), nullable=False)
    answer_json = Column(Text, nullable=False)
    evidence_source = Column(
        String(32),
        nullable=False,
        default="operator_confirmation",
        server_default="operator_confirmation",
    )
    evidence_reference = Column(
        String(255),
        nullable=False,
        default="operator_confirmation",
        server_default="operator_confirmation",
    )
    approved_by = Column(String(64), nullable=False, default="operator")
    approved_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(String(64), nullable=True)
    revocation_reason = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by IS NULL AND revocation_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoked_by IS NOT NULL "
            "AND revocation_reason IS NOT NULL)",
            name="ck_operator_approved_answers_revocation",
        ),
        CheckConstraint(
            "profile_version > 0 "
            f"AND {_sha256_check_sql('option_set_hash')} "
            f"AND {_sha256_check_sql('selected_cv_hash')} "
            f"AND {_sha256_check_sql('form_fingerprint')} "
            "AND length(trim(canonical_field)) > 0 "
            "AND length(trim(locale)) > 0 "
            "AND length(trim(policy_version)) > 0 "
            "AND length(trim(evidence_source)) > 0 "
            "AND length(trim(evidence_reference)) > 0 "
            "AND length(answer_json) BETWEEN 1 AND 4000",
            name="ck_operator_approved_answers_bounded_context",
        ),
        CheckConstraint(
            "field_contract_fingerprint IS NULL OR "
            f"{_sha256_check_sql('field_contract_fingerprint')}",
            name="ck_operator_approved_answers_field_contract",
        ),
        Index(
            "ix_operator_approved_answers_lookup",
            "canonical_field",
            "field_type",
            "option_set_hash",
            "locale",
            "profile_version",
            "selected_cv_hash",
            "adapter_name",
            "adapter_version",
            "selector_version",
            "form_fingerprint",
            "policy_version",
        ),
        Index(
            "ix_operator_approved_answers_field_contract",
            "adapter_name",
            "adapter_version",
            "selector_version",
            "field_contract_fingerprint",
            "revoked_at",
        ),
        Index("ix_operator_approved_answers_revoked_at", "revoked_at"),
    )


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
