"""SQLAlchemy ORM models for the Job Apply Agent."""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import (
    BigInteger,
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
    true,
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
    # Explicit terminal provenance prevents a later source revision from
    # reviving work that an operator or lifecycle policy already cancelled.
    terminal_skip_at = Column(DateTime, nullable=True)

    extracted_url = relationship("ExtractedURL", back_populates="jobs")
    application = relationship("Application", back_populates="job", uselist=False)
    source_occurrences = relationship(
        "JobSourceOccurrenceRecord",
        back_populates="job",
        cascade="all, delete-orphan",
    )
    fit_decisions = relationship(
        "JobFitDecisionRecord",
        back_populates="job",
        order_by="(JobFitDecisionRecord.created_at, JobFitDecisionRecord.id)",
        cascade="all, delete-orphan",
    )

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
    cv_routing_margin = Column(Float, nullable=True)
    cv_routing_evidence = Column(Text, nullable=True)
    cv_routing_fallback_reason = Column(String(64), nullable=True)
    job_fit_decision_id = Column(
        Integer,
        nullable=True,
    )
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
    job_fit_decision = relationship("JobFitDecisionRecord", viewonly=True)
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
    automation_policy_decisions = relationship(
        "ApplicationPolicyDecision",
        back_populates="application",
        order_by="(ApplicationPolicyDecision.evaluated_at, ApplicationPolicyDecision.id)",
        cascade="all, delete-orphan",
    )
    autopilot_inspection_runs = relationship(
        "AutopilotInspectionRun",
        back_populates="application",
        order_by="AutopilotInspectionRun.id",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            f"selected_cv_hash IS NULL OR {_sha256_check_sql('selected_cv_hash')}",
            name="ck_applications_selected_cv_hash",
        ),
        CheckConstraint(
            "cv_routing_margin IS NULL OR (cv_routing_margin >= 0 AND cv_routing_margin <= 1)",
            name="ck_applications_cv_routing_margin",
        ),
        ForeignKeyConstraint(
            ["job_fit_decision_id", "job_id"],
            ["job_fit_decisions.id", "job_fit_decisions.job_id"],
            name="fk_applications_job_fit_decision",
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
    authority_kind = Column(
        String(32),
        nullable=False,
        default="explicit_operator",
        server_default="explicit_operator",
    )
    automation_policy_decision_id = Column(Integer, nullable=True)
    automation_policy_decision_digest = Column(String(64), nullable=True)
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
    automation_policy_decision = relationship(
        "ApplicationPolicyDecision",
        back_populates="attempt",
        foreign_keys=[automation_policy_decision_id],
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
            "authority_kind IN ('explicit_operator', 'control_plane', "
            "'qualified_autopilot', 'legacy')",
            name="ck_submissions_authority_kind",
        ),
        CheckConstraint(
            "(authority_kind = 'qualified_autopilot' "
            "AND automation_policy_decision_id IS NOT NULL "
            "AND automation_policy_decision_digest IS NOT NULL) OR "
            "(authority_kind <> 'qualified_autopilot' "
            "AND automation_policy_decision_id IS NULL "
            "AND automation_policy_decision_digest IS NULL)",
            name="ck_submissions_automation_authority",
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
        ForeignKeyConstraint(
            ["automation_policy_decision_id", "automation_policy_decision_digest"],
            ["application_policy_decisions.id", "application_policy_decisions.decision_digest"],
            name="fk_submissions_automation_policy_decision",
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
    authority_kind = Column(
        String(32),
        nullable=False,
        default="explicit_operator",
        server_default="explicit_operator",
    )
    automation_policy_decision_digest = Column(String(64), nullable=True)

    attempt = relationship("Submission", back_populates="final_submit_permit")

    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_final_submit_permits_attempt_id"),
        UniqueConstraint("nonce_hash", name="uq_final_submit_permits_nonce_hash"),
        Index("ix_final_submit_permits_expires_at", "expires_at"),
        CheckConstraint(
            "authority_kind IN ('explicit_operator', 'control_plane', "
            "'qualified_autopilot', 'legacy')",
            name="ck_final_submit_permits_authority_kind",
        ),
        CheckConstraint(
            "(authority_kind = 'qualified_autopilot' "
            "AND automation_policy_decision_digest IS NOT NULL) OR "
            "(authority_kind <> 'qualified_autopilot' "
            "AND automation_policy_decision_digest IS NULL)",
            name="ck_final_submit_permits_automation_authority",
        ),
        CheckConstraint(
            "automation_policy_decision_digest IS NULL OR "
            f"{_sha256_check_sql('automation_policy_decision_digest')}",
            name="ck_final_submit_permits_policy_digest",
        ),
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
    revocation_state = Column(
        String(16),
        nullable=False,
        default="none",
        server_default="none",
    )
    revocation_available_at = Column(DateTime, nullable=True)
    revocation_claimed_at = Column(DateTime, nullable=True)
    revocation_claimed_by = Column(String(64), nullable=True)
    revocation_claim_token = Column(String(64), nullable=True)
    revocation_attempts = Column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    revocation_sent_at = Column(DateTime, nullable=True)
    last_revocation_error_code = Column(String(64), nullable=True)

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
        CheckConstraint(
            "revocation_state IN ('none', 'pending', 'claimed', 'delivered', 'expired') "
            "AND revocation_attempts >= 0",
            name="ck_control_plane_review_grants_revocation_state",
        ),
        CheckConstraint(
            "(revocation_state = 'none' "
            "AND revocation_available_at IS NULL "
            "AND revocation_claimed_at IS NULL "
            "AND revocation_claimed_by IS NULL "
            "AND revocation_claim_token IS NULL "
            "AND revocation_sent_at IS NULL) "
            "OR (revocation_state = 'pending' "
            "AND revocation_available_at IS NOT NULL "
            "AND revocation_claimed_at IS NULL "
            "AND revocation_claimed_by IS NULL "
            "AND revocation_claim_token IS NULL "
            "AND revocation_sent_at IS NULL) "
            "OR (revocation_state = 'claimed' "
            "AND revocation_available_at IS NOT NULL "
            "AND revocation_claimed_at IS NOT NULL "
            "AND revocation_claimed_by IS NOT NULL "
            "AND revocation_claim_token IS NOT NULL "
            "AND revocation_sent_at IS NULL) "
            "OR (revocation_state = 'delivered' "
            "AND revocation_available_at IS NOT NULL "
            "AND revocation_claimed_at IS NULL "
            "AND revocation_claimed_by IS NULL "
            "AND revocation_claim_token IS NULL "
            "AND revocation_sent_at IS NOT NULL) "
            "OR (revocation_state = 'expired' "
            "AND revocation_available_at IS NOT NULL "
            "AND revocation_claimed_at IS NULL "
            "AND revocation_claimed_by IS NULL "
            "AND revocation_claim_token IS NULL "
            "AND revocation_sent_at IS NULL)",
            name="ck_control_plane_review_grants_revocation_metadata",
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
        Index(
            "ix_control_plane_review_grants_revocation",
            "revocation_state",
            "revocation_available_at",
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
    form_contract_digest = Column(String(64), nullable=True)
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
            "qualification_tier <> 'live_canary_qualified' OR form_contract_digest IS NOT NULL",
            name="ck_browser_qualification_live_contract",
        ),
        CheckConstraint(
            f"form_contract_digest IS NULL OR {_sha256_check_sql('form_contract_digest')}",
            name="ck_browser_qualification_contract_digest",
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
    updated = Column(Integer, nullable=False, default=0, server_default="0")
    duplicates = Column(Integer, nullable=False, default=0, server_default="0")
    closed = Column(Integer, nullable=False, default=0, server_default="0")
    reason_code = Column(String(64), nullable=True)
    started_at = Column(DateTime, default=func.now(), nullable=False)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("ix_discovery_runs_source_finished", "source", "finished_at"),)


class SearchIntentRevision(Base):
    """Immutable, activated search scope derived from configured CV routes."""

    __tablename__ = "search_intent_revisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    version = Column(Integer, nullable=False, unique=True)
    schema_version = Column(String(32), nullable=False, server_default="search-intent.v1")
    payload_digest = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    active = Column(Boolean, nullable=False, default=False, server_default=false())
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    activated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            _sha256_check_sql("payload_digest"),
            name="ck_search_intent_revisions_digest",
        ),
        Index("ix_search_intent_revisions_active_version", "active", "version"),
        Index(
            "uq_search_intent_revisions_one_active",
            "active",
            unique=True,
            postgresql_where=text("active = true"),
            sqlite_where=text("active = 1"),
        ),
    )


class DiscoverySourceState(Base):
    """Operational state for one versioned discovery source."""

    __tablename__ = "discovery_sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_key = Column(String(255), nullable=False, unique=True)
    source_type = Column(String(32), nullable=False)
    descriptor_version = Column(String(32), nullable=False)
    configuration_digest = Column(String(64), nullable=False, server_default="0" * 64)
    transport = Column(String(32), nullable=False)
    authentication_mode = Column(String(32), nullable=False)
    host = Column(String(255), nullable=False)
    cadence_seconds = Column(Integer, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True, server_default=true())
    disabled_reason = Column(String(64), nullable=True)
    health_status = Column(String(24), nullable=False, default="unknown", server_default="unknown")
    next_poll_at = Column(DateTime, nullable=True)
    last_success_at = Column(DateTime, nullable=True)
    last_error_code = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    updated_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint("cadence_seconds >= 60", name="ck_discovery_sources_cadence"),
        CheckConstraint(
            _sha256_check_sql("configuration_digest"),
            name="ck_discovery_sources_configuration_digest",
        ),
        CheckConstraint(
            "(enabled = true AND disabled_reason IS NULL) OR "
            "(enabled = false AND disabled_reason IS NOT NULL)",
            name="ck_discovery_sources_enabled_reason",
        ),
        CheckConstraint(
            "health_status IN ('unknown', 'healthy', 'degraded', 'disabled')",
            name="ck_discovery_sources_health",
        ),
        Index("ix_discovery_sources_due", "enabled", "next_poll_at"),
    )


class EmployerCatalogEntryRecord(Base):
    """Tenant-scoped employer feed identifier; never implies universal coverage."""

    __tablename__ = "employer_catalog_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    catalog_key = Column(String(64), nullable=False, unique=True)
    company_name = Column(String(300), nullable=False)
    ats = Column(String(32), nullable=False)
    tenant_key = Column(String(255), nullable=False)
    region = Column(String(32), nullable=False, default="global", server_default="global")
    base_url = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default=true())
    discovered_via = Column(String(32), nullable=False, default="config", server_default="config")
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    updated_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "ats IN ('greenhouse', 'lever', 'ashby', 'smartrecruiters', "
            "'generic_jsonld', 'generic_feed')",
            name="ck_employer_catalog_entries_ats",
        ),
        CheckConstraint(
            _sha256_check_sql("catalog_key"),
            name="ck_employer_catalog_entries_key",
        ),
        UniqueConstraint("ats", "tenant_key", "region", name="uq_employer_catalog_tenant"),
        Index("ix_employer_catalog_entries_enabled_ats", "enabled", "ats"),
    )


class DiscoveryCursorState(Base):
    """Conditional-request and pagination checkpoint for one source tenant."""

    __tablename__ = "discovery_cursors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    cursor_key = Column(String(64), nullable=False, unique=True)
    source_key = Column(
        String(255),
        ForeignKey("discovery_sources.source_key", ondelete="CASCADE"),
        nullable=False,
    )
    catalog_entry_id = Column(
        Integer,
        ForeignKey("employer_catalog_entries.id", ondelete="CASCADE"),
        nullable=True,
    )
    cursor_json = Column(Text, nullable=False, default="{}", server_default="{}")
    etag = Column(String(255), nullable=True)
    last_modified = Column(String(255), nullable=True)
    last_seen_posting_at = Column(DateTime, nullable=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            _sha256_check_sql("cursor_key"),
            name="ck_discovery_cursors_key",
        ),
        Index("ix_discovery_cursors_source_catalog", "source_key", "catalog_entry_id"),
    )


class JobSourceOccurrenceRecord(Base):
    """One immutable-source identity observing a canonical local job."""

    __tablename__ = "job_source_occurrences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    occurrence_key = Column(String(64), nullable=False, unique=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    source_key = Column(String(255), nullable=False)
    catalog_entry_id = Column(
        Integer,
        ForeignKey("employer_catalog_entries.id", ondelete="SET NULL"),
        nullable=True,
    )
    external_posting_id = Column(String(255), nullable=True)
    normalized_url = Column(Text, nullable=False)
    normalized_url_hash = Column(String(64), nullable=False)
    revision_digest = Column(String(64), nullable=False)
    first_seen_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    last_seen_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())
    closed_at = Column(DateTime, nullable=True)
    active = Column(Boolean, nullable=False, default=True, server_default=true())

    job = relationship("Job", back_populates="source_occurrences")

    __table_args__ = (
        CheckConstraint(
            _sha256_check_sql("occurrence_key"),
            name="ck_job_source_occurrences_key",
        ),
        CheckConstraint(
            _sha256_check_sql("normalized_url_hash"),
            name="ck_job_source_occurrences_url_hash",
        ),
        CheckConstraint(
            _sha256_check_sql("revision_digest"),
            name="ck_job_source_occurrences_revision",
        ),
        Index("ix_job_source_occurrences_job_active", "job_id", "active"),
        Index(
            "ix_job_source_occurrences_source_external",
            "source_key",
            "external_posting_id",
        ),
        Index(
            "ix_job_source_occurrences_catalog_external",
            "catalog_entry_id",
            "external_posting_id",
        ),
        Index("ix_job_source_occurrences_last_seen", "source_key", "last_seen_at"),
    )


class JobFitDecisionRecord(Base):
    """Immutable, privacy-bounded fit decision for one job/profile/CV snapshot."""

    __tablename__ = "job_fit_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    decision_digest = Column(String(64), nullable=False)
    job_digest = Column(String(64), nullable=False)
    profile_version = Column(Integer, nullable=True)
    routing_config_digest = Column(String(64), nullable=False)
    cv_manifest_digest = Column(String(64), nullable=False)
    selected_cv_id = Column(String(255), nullable=True)
    selected_cv_hash = Column(String(64), nullable=True)
    routing_confidence = Column(Float, nullable=False)
    routing_margin = Column(Float, nullable=False)
    routing_fallback_reason = Column(String(64), nullable=True)
    fit_score = Column(Float, nullable=False)
    disposition = Column(String(24), nullable=False)
    quality_eligible = Column(Boolean, nullable=False, default=False, server_default=false())
    hard_exclusions_json = Column(Text, nullable=False, default="[]", server_default="[]")
    uncertainty_json = Column(Text, nullable=False, default="[]", server_default="[]")
    unsupported_skills_json = Column(Text, nullable=False, default="[]", server_default="[]")
    evidence_json = Column(Text, nullable=False)
    thresholds_json = Column(Text, nullable=False)
    policy_version = Column(String(32), nullable=False)
    model_identity = Column(String(64), nullable=False)
    qualification_digest = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    job = relationship("Job", back_populates="fit_decisions")

    __table_args__ = (
        UniqueConstraint(
            "job_id",
            "decision_digest",
            name="uq_job_fit_decisions_job_digest",
        ),
        UniqueConstraint(
            "id",
            "job_id",
            name="uq_job_fit_decisions_id_job",
        ),
        CheckConstraint(
            f"{_sha256_check_sql('decision_digest')} "
            f"AND {_sha256_check_sql('job_digest')} "
            f"AND {_sha256_check_sql('routing_config_digest')} "
            f"AND {_sha256_check_sql('cv_manifest_digest')}",
            name="ck_job_fit_decisions_required_digests",
        ),
        CheckConstraint(
            f"selected_cv_hash IS NULL OR {_sha256_check_sql('selected_cv_hash')}",
            name="ck_job_fit_decisions_selected_cv_hash",
        ),
        CheckConstraint(
            f"qualification_digest IS NULL OR {_sha256_check_sql('qualification_digest')}",
            name="ck_job_fit_decisions_qualification_digest",
        ),
        CheckConstraint(
            "routing_confidence >= 0 AND routing_confidence <= 1 "
            "AND routing_margin >= 0 AND routing_margin <= 1 "
            "AND fit_score >= 0 AND fit_score <= 100",
            name="ck_job_fit_decisions_metrics",
        ),
        CheckConstraint(
            "disposition IN ('excluded', 'needs_review', 'eligible')",
            name="ck_job_fit_decisions_disposition",
        ),
        CheckConstraint(
            "quality_eligible = false OR "
            "(disposition = 'eligible' AND selected_cv_id IS NOT NULL "
            "AND selected_cv_hash IS NOT NULL AND qualification_digest IS NOT NULL)",
            name="ck_job_fit_decisions_eligibility",
        ),
        CheckConstraint(
            "profile_version IS NULL OR profile_version > 0",
            name="ck_job_fit_decisions_profile_version",
        ),
        Index("ix_job_fit_decisions_job_created", "job_id", "created_at", "id"),
        Index(
            "ix_job_fit_decisions_disposition_created",
            "disposition",
            "quality_eligible",
            "created_at",
        ),
    )


class AutomationPolicyRevisionRecord(Base):
    """Locally signed, immutable policy payload with explicit revocation state."""

    __tablename__ = "automation_policy_revisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_id = Column(String(36), nullable=False)
    revision = Column(Integer, nullable=False)
    schema_version = Column(String(32), nullable=False)
    payload_json = Column(Text, nullable=False)
    payload_digest = Column(String(64), nullable=False)
    signing_key_id = Column(String(36), nullable=False)
    signature = Column(String(86), nullable=False)
    active_slot = Column(Integer, nullable=True)
    activated_at = Column(DateTime, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    revoked_by = Column(String(32), nullable=True)
    revocation_reason = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    decisions = relationship(
        "ApplicationPolicyDecision",
        back_populates="policy_revision",
    )
    inspection_runs = relationship(
        "AutopilotInspectionRun",
        back_populates="policy_revision",
    )

    __table_args__ = (
        UniqueConstraint(
            "policy_id",
            "revision",
            name="uq_automation_policy_revisions_identity",
        ),
        UniqueConstraint("payload_digest", name="uq_automation_policy_revisions_digest"),
        UniqueConstraint("active_slot", name="uq_automation_policy_revisions_active_slot"),
        CheckConstraint(
            "schema_version = 'auto-submit-policy.v1' "
            "AND revision > 0 "
            "AND expires_at > activated_at "
            "AND (active_slot IS NULL OR active_slot = 1)",
            name="ck_automation_policy_revisions_core",
        ),
        CheckConstraint(
            f"{_sha256_check_sql('payload_digest')} "
            "AND length(signature) = 86 "
            "AND length(payload_json) BETWEEN 2 AND 32768",
            name="ck_automation_policy_revisions_crypto",
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_by IS NULL "
            "AND revocation_reason IS NULL AND active_slot = 1) OR "
            "(revoked_at IS NOT NULL AND revoked_by IS NOT NULL "
            "AND revocation_reason IS NOT NULL AND active_slot IS NULL)",
            name="ck_automation_policy_revisions_revocation",
        ),
        Index("ix_automation_policy_revisions_expiry", "expires_at"),
    )


class AutopilotInspectionRun(Base):
    """Durable, reversible inspection lease for one exact policy revision."""

    __tablename__ = "autopilot_inspection_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    application_revision = Column(Integer, nullable=False)
    policy_revision_id = Column(
        Integer,
        ForeignKey("automation_policy_revisions.id"),
        nullable=False,
    )
    state = Column(String(16), nullable=False, default="queued", server_default="queued")
    reason_code = Column(String(64), nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    claim_token = Column(String(36), nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    application = relationship("Application", back_populates="autopilot_inspection_runs")
    policy_revision = relationship(
        "AutomationPolicyRevisionRecord",
        back_populates="inspection_runs",
    )

    __table_args__ = (
        UniqueConstraint(
            "application_id",
            "application_revision",
            "policy_revision_id",
            name="uq_autopilot_inspection_runs_exact",
        ),
        CheckConstraint(
            "application_revision > 0 AND state IN ('queued', 'running', 'finished')",
            name="ck_autopilot_inspection_runs_core",
        ),
        CheckConstraint(
            "(state = 'queued' AND claimed_at IS NULL AND lease_expires_at IS NULL "
            "AND claim_token IS NULL "
            "AND finished_at IS NULL AND reason_code IS NULL) OR "
            "(state = 'running' AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND claim_token IS NOT NULL AND lease_expires_at > claimed_at AND finished_at IS NULL "
            "AND reason_code IS NULL) OR "
            "(state = 'finished' AND claimed_at IS NOT NULL AND lease_expires_at IS NULL "
            "AND claim_token IS NULL "
            "AND finished_at IS NOT NULL AND reason_code IS NOT NULL)",
            name="ck_autopilot_inspection_runs_state",
        ),
        Index(
            "ix_autopilot_inspection_runs_claim",
            "state",
            "lease_expires_at",
            "created_at",
        ),
    )


class ApplicationPolicyDecision(Base):
    """One redacted, immutable policy result and optional cap reservation."""

    __tablename__ = "application_policy_decisions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    policy_revision_id = Column(
        Integer,
        ForeignKey("automation_policy_revisions.id"),
        nullable=False,
    )
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    application_revision = Column(Integer, nullable=False)
    fit_decision_id = Column(Integer, ForeignKey("job_fit_decisions.id"), nullable=False)
    form_plan_id = Column(Integer, ForeignKey("form_plans.id"), nullable=False)
    decision_digest = Column(String(64), nullable=False)
    policy_digest = Column(String(64), nullable=False)
    job_digest = Column(String(64), nullable=False)
    company_digest = Column(String(64), nullable=False)
    fit_decision_digest = Column(String(64), nullable=False)
    form_plan_public_id = Column(String(36), nullable=False)
    form_fingerprint = Column(String(64), nullable=False)
    form_contract_digest = Column(String(64), nullable=False)
    selected_cv_hash = Column(String(64), nullable=False)
    profile_version = Column(Integer, nullable=False)
    confirmed_answer_revision = Column(String(64), nullable=False)
    adapter_name = Column(String(64), nullable=False)
    adapter_version = Column(String(32), nullable=False)
    selector_version = Column(String(64), nullable=False)
    fit_score = Column(Float, nullable=False)
    allowed = Column(Boolean, nullable=False, default=False, server_default=false())
    reason_codes_json = Column(Text, nullable=False, default="[]", server_default="[]")
    authority_expires_at = Column(DateTime, nullable=True)
    evaluated_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    policy_revision = relationship(
        "AutomationPolicyRevisionRecord",
        back_populates="decisions",
    )
    application = relationship("Application", back_populates="automation_policy_decisions")
    attempt = relationship(
        "Submission",
        back_populates="automation_policy_decision",
        uselist=False,
        foreign_keys="Submission.automation_policy_decision_id",
    )

    __table_args__ = (
        UniqueConstraint(
            "id",
            "decision_digest",
            name="uq_application_policy_decisions_id_digest",
        ),
        UniqueConstraint(
            "application_id",
            "policy_revision_id",
            "application_revision",
            "form_plan_id",
            "decision_digest",
            name="uq_application_policy_decisions_exact",
        ),
        Index(
            "uq_application_policy_decisions_one_allowed",
            "application_id",
            unique=True,
            postgresql_where=text("allowed = true"),
            sqlite_where=text("allowed = 1"),
        ),
        Index(
            "ix_application_policy_decisions_limits",
            "allowed",
            "evaluated_at",
            "company_digest",
        ),
        CheckConstraint(
            "application_revision > 0 AND profile_version > 0 "
            "AND fit_score >= 0 AND fit_score <= 100",
            name="ck_application_policy_decisions_metrics",
        ),
        CheckConstraint(
            f"{_sha256_check_sql('decision_digest')} "
            f"AND {_sha256_check_sql('policy_digest')} "
            f"AND {_sha256_check_sql('job_digest')} "
            f"AND {_sha256_check_sql('company_digest')} "
            f"AND {_sha256_check_sql('fit_decision_digest')} "
            f"AND {_sha256_check_sql('form_fingerprint')} "
            f"AND {_sha256_check_sql('form_contract_digest')} "
            f"AND {_sha256_check_sql('selected_cv_hash')} "
            f"AND {_sha256_check_sql('confirmed_answer_revision')}",
            name="ck_application_policy_decisions_digests",
        ),
        CheckConstraint(
            "(allowed = true AND reason_codes_json = '[]' "
            "AND authority_expires_at IS NOT NULL "
            "AND authority_expires_at > evaluated_at) OR "
            "(allowed = false AND reason_codes_json <> '[]' "
            "AND authority_expires_at IS NULL)",
            name="ck_application_policy_decisions_outcome",
        ),
    )


class AutomationKillSwitchEvent(Base):
    """Append-only local or signed-remote kill-switch state transition."""

    __tablename__ = "automation_kill_switch_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    revision = Column(Integer, nullable=False)
    active = Column(Boolean, nullable=False)
    source = Column(String(32), nullable=False)
    reason_code = Column(String(64), nullable=False)
    command_digest = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now(), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("revision", name="uq_automation_kill_switch_revision"),
        UniqueConstraint("command_digest", name="uq_automation_kill_switch_command"),
        CheckConstraint(
            "revision > 0 AND source IN ('local_operator', 'vercel_signed_kill') "
            "AND length(trim(reason_code)) BETWEEN 2 AND 64",
            name="ck_automation_kill_switch_core",
        ),
        CheckConstraint(
            f"command_digest IS NULL OR {_sha256_check_sql('command_digest')}",
            name="ck_automation_kill_switch_command_digest",
        ),
        CheckConstraint(
            "source <> 'vercel_signed_kill' OR (active = true AND command_digest IS NOT NULL)",
            name="ck_automation_kill_switch_remote_only_stops",
        ),
        Index("ix_automation_kill_switch_created", "created_at", "id"),
    )


class OperationalMetricReceipt(Base):
    """Permanent privacy-safe receipt preserving metric idempotency.

    Receipts contain only a one-way event digest and its recording time. They
    remain after bounded event detail is pruned so delayed task redelivery can
    never increment a cumulative rollup twice.
    """

    __tablename__ = "operational_metric_receipts"

    event_key = Column(String(64), primary_key=True)
    recorded_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            _sha256_check_sql("event_key"),
            name="ck_operational_metric_receipts_event_key",
        ),
    )


class OperationalMetricEvent(Base):
    """Immutable, deduplicated, privacy-safe operational metric event."""

    __tablename__ = "operational_metric_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(
        String(64),
        ForeignKey(
            "operational_metric_receipts.event_key",
            name="fk_operational_metric_events_receipt",
        ),
        nullable=False,
    )
    entity_key = Column(String(64), nullable=False)
    metric_name = Column(String(32), nullable=False)
    ats = Column(String(32), nullable=False, default="none", server_default="none")
    adapter_version = Column(String(32), nullable=False, default="none", server_default="none")
    selector_version = Column(String(64), nullable=False, default="none", server_default="none")
    stage = Column(String(24), nullable=False, default="none", server_default="none")
    outcome = Column(String(32), nullable=False, default="none", server_default="none")
    reason_code = Column(String(64), nullable=False, default="NONE", server_default="NONE")
    field_type = Column(String(24), nullable=False, default="none", server_default="none")
    resolver = Column(String(40), nullable=False, default="none", server_default="none")
    attachment_result = Column(
        String(24),
        nullable=False,
        default="none",
        server_default="none",
    )
    evidence_type = Column(String(48), nullable=False, default="none", server_default="none")
    duration_ms = Column(Integer, nullable=True)
    occurred_at = Column(DateTime, nullable=False)
    created_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("event_key", name="uq_operational_metric_events_event_key"),
        CheckConstraint(
            f"{_sha256_check_sql('event_key')} AND {_sha256_check_sql('entity_key')}",
            name="ck_operational_metric_events_digests",
        ),
        CheckConstraint(
            "metric_name IN ('attempt_stage', 'attempt_outcome', 'retry', "
            "'governor_denial', 'discovery_result', 'form_resolution', "
            "'attachment_result', 'browser_failure', 'outbound_result')",
            name="ck_operational_metric_events_name",
        ),
        CheckConstraint(
            "duration_ms IS NULL OR duration_ms BETWEEN 0 AND 604800000",
            name="ck_operational_metric_events_duration",
        ),
        CheckConstraint(
            "length(ats) BETWEEN 1 AND 32 "
            "AND length(adapter_version) BETWEEN 1 AND 32 "
            "AND length(selector_version) BETWEEN 1 AND 64 "
            "AND length(stage) BETWEEN 1 AND 24 "
            "AND length(outcome) BETWEEN 1 AND 32 "
            "AND length(reason_code) BETWEEN 1 AND 64 "
            "AND length(field_type) BETWEEN 1 AND 24 "
            "AND length(resolver) BETWEEN 1 AND 40 "
            "AND length(attachment_result) BETWEEN 1 AND 24 "
            "AND length(evidence_type) BETWEEN 1 AND 48",
            name="ck_operational_metric_events_label_lengths",
        ),
        Index("ix_operational_metric_events_metric_time", "metric_name", "occurred_at"),
        Index("ix_operational_metric_events_entity_time", "entity_key", "occurred_at"),
        Index("ix_operational_metric_events_occurred_at", "occurred_at"),
        Index(
            "ix_operational_metric_events_failure_cluster",
            "ats",
            "adapter_version",
            "selector_version",
            "reason_code",
            "occurred_at",
        ),
    )


class OperationalMetricRollup(Base):
    """Cumulative non-personal metric totals shared by API and workers."""

    __tablename__ = "operational_metric_rollups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    metric_name = Column(String(32), nullable=False)
    ats = Column(String(32), nullable=False, default="none", server_default="none")
    adapter_version = Column(String(32), nullable=False, default="none", server_default="none")
    selector_version = Column(String(64), nullable=False, default="none", server_default="none")
    stage = Column(String(24), nullable=False, default="none", server_default="none")
    outcome = Column(String(32), nullable=False, default="none", server_default="none")
    reason_code = Column(String(64), nullable=False, default="NONE", server_default="NONE")
    field_type = Column(String(24), nullable=False, default="none", server_default="none")
    resolver = Column(String(40), nullable=False, default="none", server_default="none")
    attachment_result = Column(
        String(24),
        nullable=False,
        default="none",
        server_default="none",
    )
    evidence_type = Column(String(48), nullable=False, default="none", server_default="none")
    event_count = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_count = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_sum_ms = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_le_1s = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_le_5s = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_le_15s = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_le_60s = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_le_300s = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_le_900s = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    duration_le_inf = Column(BigInteger, nullable=False, default=0, server_default=text("0"))
    updated_at = Column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "metric_name",
            "ats",
            "adapter_version",
            "selector_version",
            "stage",
            "outcome",
            "reason_code",
            "field_type",
            "resolver",
            "attachment_result",
            "evidence_type",
            name="uq_operational_metric_rollups_dimensions",
        ),
        CheckConstraint(
            "metric_name IN ('attempt_stage', 'attempt_outcome', 'retry', "
            "'governor_denial', 'discovery_result', 'form_resolution', "
            "'attachment_result', 'browser_failure', 'outbound_result')",
            name="ck_operational_metric_rollups_name",
        ),
        CheckConstraint(
            "length(ats) BETWEEN 1 AND 32 "
            "AND length(adapter_version) BETWEEN 1 AND 32 "
            "AND length(selector_version) BETWEEN 1 AND 64 "
            "AND length(stage) BETWEEN 1 AND 24 "
            "AND length(outcome) BETWEEN 1 AND 32 "
            "AND length(reason_code) BETWEEN 1 AND 64 "
            "AND length(field_type) BETWEEN 1 AND 24 "
            "AND length(resolver) BETWEEN 1 AND 40 "
            "AND length(attachment_result) BETWEEN 1 AND 24 "
            "AND length(evidence_type) BETWEEN 1 AND 48",
            name="ck_operational_metric_rollups_label_lengths",
        ),
        CheckConstraint(
            "event_count >= 0 AND duration_count >= 0 AND duration_sum_ms >= 0 "
            "AND duration_le_1s >= 0 AND duration_le_5s >= duration_le_1s "
            "AND duration_le_15s >= duration_le_5s "
            "AND duration_le_60s >= duration_le_15s "
            "AND duration_le_300s >= duration_le_60s "
            "AND duration_le_900s >= duration_le_300s "
            "AND duration_le_inf >= duration_le_900s "
            "AND duration_le_inf = duration_count",
            name="ck_operational_metric_rollups_totals",
        ),
        Index("ix_operational_metric_rollups_metric", "metric_name"),
        Index(
            "ix_operational_metric_rollups_failure_cluster",
            "ats",
            "adapter_version",
            "selector_version",
            "reason_code",
        ),
    )


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
