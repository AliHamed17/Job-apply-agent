"""Database session and engine factory."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from core.config import get_settings

_engine = None
_SessionLocal = None


def get_engine():
    """Lazily create the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        if settings.db_is_sqlite:
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.database_url,
            connect_args=connect_args,
            echo=False,
        )
    return _engine


def get_session_factory() -> sessionmaker:
    """Get (or create) the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(),
        )
    return _SessionLocal


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency — yields a DB session and cleans up after."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create all tables (for development / MVP). Use Alembic in production."""
    from db.models import Base  # noqa: F811

    Base.metadata.create_all(bind=get_engine())
    _run_sqlite_safe_migrations()
    _backfill_job_platforms()


def _run_sqlite_safe_migrations() -> None:
    """Best-effort schema backfills for SQLite dev environments."""
    engine = get_engine()
    if engine.dialect.name != "sqlite":
        return

    with engine.begin() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(jobs)"))}
        if "platform" not in cols:
            conn.execute(text("ALTER TABLE jobs ADD COLUMN platform VARCHAR(50)"))


def _backfill_job_platforms() -> None:
    """Backfill platform for existing jobs if missing."""
    from ingestion.url_utils import identify_job_platform

    session_factory = get_session_factory()
    db = session_factory()
    try:
        from db.models import Job

        rows = db.query(Job).filter(Job.platform.is_(None)).all()
        for job in rows:
            job.platform = identify_job_platform(job.apply_url or job.source_url)
        db.commit()
    finally:
        db.close()
