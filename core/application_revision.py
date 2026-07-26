"""Application revision and preparation invalidation helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from db.models import Application, FormPlan


def application_revision(application: Application) -> int:
    """Return a positive revision for legacy rows and newly created drafts."""
    return max(1, int(application.revision or 1))


def invalidate_form_plans(
    db,
    application: Application,
    *,
    reason_code: str,
    now: datetime | None = None,
) -> int:
    """Invalidate every unconsumed plan after private application content changes."""
    timestamp = now or datetime.now(UTC).replace(tzinfo=None)
    plans = (
        db.query(FormPlan)
        .filter(
            FormPlan.application_id == application.id,
            FormPlan.invalidated_at.is_(None),
        )
        .all()
    )
    for plan in plans:
        plan.invalidated_at = timestamp
        plan.invalidation_reason = reason_code[:64]
    return len(plans)


def bump_application_revision(
    db,
    application: Application,
    *,
    reason_code: str,
    now: datetime | None = None,
) -> int:
    """Advance content identity and revoke every prior review/plan binding."""
    application.revision = application_revision(application) + 1
    application.prepared_revision = None
    application.approved_at = None
    application.approval_source = None
    invalidate_form_plans(
        db,
        application,
        reason_code=reason_code,
        now=now,
    )
    return application.revision


def mark_application_prepared(application: Application) -> int:
    """Bind operator preparation to the exact current private-content revision."""
    revision = application_revision(application)
    application.revision = revision
    application.prepared_revision = revision
    return revision


def preparation_is_current(application: Application) -> bool:
    """Return whether review still matches the application content."""
    revision = application_revision(application)
    return (
        application.approved_at is not None
        and application.prepared_revision is not None
        and application.prepared_revision == revision
    )
