# ── Stage 1: base ──────────────────────────────────────────────────────────
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

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
# Install the package without editable mode (copies sources later)
RUN pip install --upgrade pip && \
    pip install ".[pdf]"

# ── Stage 3: web-api ───────────────────────────────────────────────────────
FROM deps AS web-api

COPY . .
RUN pip install -e ".[pdf]"

# Run DB migrations then start Uvicorn
CMD ["sh", "-c", "alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port 8000"]

# ── Stage 4: celery-worker ─────────────────────────────────────────────────
FROM deps AS celery-worker

COPY . .
RUN pip install -e ".[pdf]"

# Each Celery worker handles all queues by default; override via CELERY_QUEUES env var
CMD ["celery", "-A", "worker.celery_app", "worker", \
     "--loglevel=info", \
     "--concurrency=2", \
     "--queues=ingestion,processing,llm,submission"]
