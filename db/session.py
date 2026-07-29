"""Database session and engine factory."""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import Settings, get_settings

_engine = None
_SessionLocal = None


def create_engine_for_settings(settings: Settings) -> Engine:
    """Create an engine bound to one already-validated settings object."""

    connect_args = {}
    if settings.db_is_sqlite:
        connect_args["check_same_thread"] = False
    engine = create_engine(
        settings.database_url,
        connect_args=connect_args,
        echo=False,
    )
    if settings.db_is_sqlite:

        @event.listens_for(engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

    return engine


def create_session_factory_for_engine(engine: Engine) -> sessionmaker:
    """Create a session factory for an explicit engine."""

    return sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
    )


def get_engine():
    """Lazily create the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        _engine = create_engine_for_settings(get_settings())
    return _engine


def get_session_factory() -> sessionmaker:
    """Get (or create) the session factory."""
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = create_session_factory_for_engine(get_engine())
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
