"""Alembic environment configuration.

Reads DATABASE_URL from the application's Settings so that the same env var
used by FastAPI and Celery is also used during migrations:

    alembic upgrade head           # uses DATABASE_URL from .env
    DATABASE_URL=postgresql://... alembic upgrade head  # override inline
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ── Load application settings ─────────────────────────────────────────────
# Import triggers .env loading via pydantic-settings
from core.config import get_settings

settings = get_settings()

# ── Alembic config object ─────────────────────────────────────────────────
config = context.config

# Override the sqlalchemy.url from alembic.ini with the runtime DATABASE_URL
config.set_main_option("sqlalchemy.url", settings.database_url)

# ── Logging ───────────────────────────────────────────────────────────────
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# ── Target metadata (all ORM models) ─────────────────────────────────────
from db.models import Base  # noqa: E402 — must be after settings load

target_metadata = Base.metadata


# ── Offline migration (no live DB connection) ─────────────────────────────
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Generates SQL scripts without connecting to the database.  Useful for
    review and for environments where the DB is not accessible during CI.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Include schemas if using PostgreSQL with non-default schemas
        # include_schemas=True,
    )

    with context.begin_transaction():
        context.run_migrations()


# ── Online migration (live DB connection) ─────────────────────────────────
def run_migrations_online() -> None:
    """Run migrations in 'online' mode with a live DB connection."""
    # For SQLite: disable connection pooling (single-writer)
    connect_args: dict = {}
    poolclass = pool.NullPool
    if settings.db_is_sqlite:
        connect_args["check_same_thread"] = False

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=poolclass,
        connect_args=connect_args,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Detect column-type changes (important for Postgres migrations)
            compare_type=True,
            # Render column defaults in generated SQL
            render_as_batch=settings.db_is_sqlite,  # required for SQLite ALTER TABLE
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
