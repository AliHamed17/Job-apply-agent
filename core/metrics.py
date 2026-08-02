"""Bounded-cardinality Prometheus metrics.

The production image installs ``prometheus_client``.  Keeping a tiny no-op
implementation here also lets configuration checks and minimal smoke
environments import the application without weakening the metrics API used by
the rest of the codebase.
"""

try:
    from prometheus_client import Counter, Gauge, Histogram
except ImportError:  # pragma: no cover - exercised in dependency-light smokes

    class _NoOpMetric:
        def __init__(self, name: str, documentation: str, labelnames=()):
            self.name = name
            self.documentation = documentation
            self.labelnames = tuple(labelnames)

        def labels(self, *args, **kwargs):
            return self

        def inc(self, amount: float = 1) -> None:
            return None

        def observe(self, amount: float) -> None:
            return None

        def set(self, value: float) -> None:
            return None

    Counter = Gauge = Histogram = _NoOpMetric

HTTP_METHOD_LABELS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


def normalize_http_method(value: object) -> str:
    candidate = str(value or "").strip().upper()
    return candidate if candidate in HTTP_METHOD_LABELS else "OTHER"


HTTP_REQUESTS = Counter(
    "job_agent_http_requests_total",
    "HTTP requests by normalized route, method, and response class.",
    ["route", "method", "status_class"],
)
HTTP_LATENCY = Histogram(
    "job_agent_http_request_duration_seconds",
    "HTTP request latency by normalized route.",
    ["route"],
)
PIPELINE_LATENCY = Histogram(
    "job_agent_pipeline_duration_seconds",
    "Pipeline task latency.",
    ["stage"],
)
FAILURES = Counter(
    "job_agent_failures_total",
    "Failures by stable component and reason code.",
    ["component", "reason_code"],
)
RETRIES = Counter("job_agent_retries_total", "Retry attempts.", ["component"])
GOVERNOR_DENIALS = Counter(
    "job_agent_governor_denials_total", "Governor denials by stable reason.", ["reason"]
)
QUEUE_DEPTH = Gauge("job_agent_queue_depth", "Current queue depth.", ["queue"])
QUEUE_SNAPSHOT_AVAILABLE = Gauge(
    "job_agent_queue_snapshot_available",
    "Whether the queue gauge was refreshed from authoritative database state.",
)
CHALLENGE_TRIPS = Counter(
    "job_agent_challenge_trips_total", "Detected browser challenges.", ["platform"]
)
OUTBOUND_RESULTS = Counter(
    "job_agent_outbound_results_total", "Outbound results.", ["channel", "result"]
)
SELECTOR_FAILURES = Counter(
    "job_agent_selector_failures_total",
    "Browser flow failures by selector version and stable reason.",
    ["selector_version", "reason"],
)


def refresh_authoritative_metrics() -> None:
    """Refresh fixed queue gauges from shared database state for this scrape."""

    from core.operational_labels import QUEUE_LABELS

    values = {name: float("nan") for name in QUEUE_LABELS}
    available = 0
    db = None
    try:
        from core.operational_metrics import authoritative_queue_depths
        from db.session import get_session_factory

        db = get_session_factory()()
        values = authoritative_queue_depths(db)
        available = 1
    except Exception:
        if db is not None:
            db.rollback()
    finally:
        if db is not None:
            db.close()

    for name in QUEUE_LABELS:
        QUEUE_DEPTH.labels(queue=name).set(values[name])
    QUEUE_SNAPSHOT_AVAILABLE.set(available)


from core.operational_metrics import register_durable_operational_collector  # noqa: E402
from core.v5_operational_metrics import register_v5_operational_collector  # noqa: E402

register_durable_operational_collector()
register_v5_operational_collector()
