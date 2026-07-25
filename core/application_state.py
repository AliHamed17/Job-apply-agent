"""Compatibility queries for reviewable and prepared applications.

PR1 records preparation without using ``APPROVED``, because that legacy state
was consumed by an unattended submission drainer. These helpers keep every
reader aligned until the domain migration introduces a dedicated stage.
"""

from __future__ import annotations

from sqlalchemy import and_, or_

from db.models import Application, JobStatus


def reviewable_applications_query(db):
    """Return drafts that have not yet been prepared by an operator."""
    return db.query(Application).filter(
        Application.status == JobStatus.DRAFT,
        Application.approved_at.is_(None),
    )


def prepared_applications_query(db):
    """Return PR1 prepared drafts plus legacy approved compatibility rows."""
    return db.query(Application).filter(
        or_(
            Application.status == JobStatus.APPROVED,
            and_(
                Application.status == JobStatus.DRAFT,
                Application.approved_at.isnot(None),
            ),
        )
    )


def reviewable_application_count(db) -> int:
    return reviewable_applications_query(db).count()


def prepared_application_count(db) -> int:
    return prepared_applications_query(db).count()
