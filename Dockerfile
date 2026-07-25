# ── Stage 1: base ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# System deps for lxml, pdfminer, and httpx
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt-dev \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── Stage 2: deps ──────────────────────────────────────────────────────────
FROM base AS deps

COPY pyproject.toml ./
# Install the package without editable mode (copies sources later).
# `email` extra (aiosmtplib) is required at runtime: text-post ingestion in
# web-api sends the CV by email when a recruiter post has only an email
# contact — without it that send is swallowed and reported as no_contact.
RUN pip install --upgrade pip && \
    pip install ".[pdf,email,postgres]"

# ── Stage 3: web-api ───────────────────────────────────────────────────────
FROM deps AS web-api

COPY . .
RUN pip install -e ".[pdf,email,postgres]"

# Run DB migrations then start Uvicorn
CMD ["sh", "-c", "mkdir -p /app/profile-data && if [ ! -f /app/profile-data/user_profile.yaml ]; then cp /app/user_profile.yaml.example /app/profile-data/user_profile.yaml; fi && alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port 8000"]

# ── Stage 4: celery-worker ─────────────────────────────────────────────────
# celery-beat (docker-compose) builds from this same stage — it schedules
# discover_jobs_task, and a worker on the "discovery" queue then imports
# playwright to run it, so both need the browser extra + Chromium installed.
FROM deps AS celery-worker

COPY . .
RUN pip install -e ".[pdf,browser,email,postgres]" && \
    playwright install --with-deps chromium

# Each Celery worker handles all queues by default; override via CELERY_QUEUES env var
CMD ["celery", "-A", "worker.celery_app", "worker", \
     "--loglevel=info", \
     "--concurrency=2", \
     "--queues=ingestion,processing,llm,submission"]
