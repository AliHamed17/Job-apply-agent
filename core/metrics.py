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
