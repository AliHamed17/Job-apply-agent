"""Authoritative, privacy-bounded v5 operating metrics.

The durable operational collector remains the event-level audit surface.  This
collector adds the small set of v5 pipeline views that are most useful to an
operator and awkward to infer in PromQL: source lag and feed outcomes,
deduplication totals, current fit disposition, calibrated qualification
ratios, signed-policy decisions, and exact employer-confirmed applications.

All labels are selected from finite allowlists.  Source keys, tenants,
companies, job titles and URLs, CV identifiers, profile data, and free-form
errors never cross this boundary.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func

from core.operational_labels import normalize_ats, normalize_outcome, normalize_reason_code
from core.submission_truth import latest_employer_verified_query
from db.models import (
    ApplicationPolicyDecision,
    DiscoveryRun,
    DiscoverySourceState,
    JobFitDecisionRecord,
    OperationalMetricRollup,
    Submission,
)

DISCOVERY_SOURCE_LABELS = frozenset(
    {
        "ashby",
        "generic_feed",
        "generic_jsonld",
        "gmail_alert",
        "greenhouse",
        "lever",
        "linkedin_partner",
        "remotive",
        "smartrecruiters",
        "other",
    }
)
DISCOVERY_HEALTH_LABELS = frozenset({"healthy", "degraded", "unknown", "disabled"})
DISCOVERY_RUN_RESULT_LABELS = frozenset({"success", "failed", "running", "other"})
FIT_DISPOSITION_LABELS = frozenset({"eligible", "needs_review", "excluded", "other"})
QUALIFICATION_RATIO_LABELS = ("precision", "coverage", "abstention")
POLICY_DECISION_LABELS = ("allowed", "denied")
DISCOVERY_POSTING_RESULT_LABELS = ("inserted", "updated", "duplicate", "closed")


def normalize_discovery_source(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in DISCOVERY_SOURCE_LABELS else "other"


def normalize_discovery_health(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in DISCOVERY_HEALTH_LABELS else "unknown"


def normalize_discovery_run_result(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in DISCOVERY_RUN_RESULT_LABELS else "other"


def normalize_fit_disposition(value: object) -> str:
    candidate = str(value or "").strip().lower()
    return candidate if candidate in FIT_DISPOSITION_LABELS else "other"


def _naive_utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        return current
    return current.astimezone(UTC).replace(tzinfo=None)


def _source_lookup(states) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for row in states:
        key = str(row.source_key or "")[:64]
        source = normalize_discovery_source(row.source_type)
        existing = lookup.get(key)
        lookup[key] = source if existing in {None, source} else "other"
    return lookup


def _source_for_run(value: object, lookup: dict[str, str]) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in lookup:
        return lookup[candidate]
    direct = normalize_discovery_source(candidate)
    if direct != "other":
        return direct
    prefix = candidate.split(":", 1)[0]
    return normalize_discovery_source(prefix)


def _policy_reasons(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return ("OTHER",)
    if not isinstance(parsed, list) or not parsed:
        return ("OTHER",)
    normalized = {normalize_reason_code(item) for item in parsed}
    normalized.discard("NONE")
    return tuple(sorted(normalized or {"OTHER"}))


def _qualification_snapshot() -> dict[str, Any]:
    try:
        from match.job_fit import load_fit_qualification
        from match.job_fit_runtime import configured_fit_qualification_path

        qualification = load_fit_qualification(configured_fit_qualification_path())
        precision = float(qualification.holdout_precision)
        coverage = float(qualification.holdout_coverage)
        if not all(math.isfinite(item) and 0.0 <= item <= 1.0 for item in (precision, coverage)):
            raise ValueError("non-finite qualification ratio")
        return {
            "available": 1,
            "qualified": int(qualification.qualified),
            "ratios": {
                "precision": precision,
                "coverage": coverage,
                "abstention": 1.0 - coverage,
            },
        }
    except Exception:
        return {"available": 0, "qualified": 0, "ratios": {}}


def _database_snapshot(db, *, now: datetime) -> dict[str, Any]:
    source_states = db.query(
        DiscoverySourceState.source_key,
        DiscoverySourceState.source_type,
        DiscoverySourceState.enabled,
        DiscoverySourceState.health_status,
        DiscoverySourceState.last_success_at,
    ).all()
    lookup = _source_lookup(source_states)

    source_instances: defaultdict[tuple[str, str], int] = defaultdict(int)
    source_enabled: defaultdict[str, int] = defaultdict(int)
    source_success_available: defaultdict[str, int] = defaultdict(int)
    source_lag: defaultdict[str, float] = defaultdict(float)
    for row in source_states:
        source = normalize_discovery_source(row.source_type)
        health = normalize_discovery_health(row.health_status)
        source_instances[(source, health)] += 1
        if not row.enabled:
            continue
        source_enabled[source] += 1
        if row.last_success_at is not None:
            source_success_available[source] += 1
            lag = max(0.0, (now - _naive_utc(row.last_success_at)).total_seconds())
            source_lag[source] = max(source_lag[source], lag)

    run_rows = (
        db.query(
            DiscoveryRun.source,
            DiscoveryRun.status,
            DiscoveryRun.reason_code,
            func.count(DiscoveryRun.id).label("run_count"),
            func.coalesce(func.sum(DiscoveryRun.inserted), 0).label("inserted"),
            func.coalesce(func.sum(DiscoveryRun.updated), 0).label("updated"),
            func.coalesce(func.sum(DiscoveryRun.duplicates), 0).label("duplicates"),
            func.coalesce(func.sum(DiscoveryRun.closed), 0).label("closed"),
        )
        .group_by(DiscoveryRun.source, DiscoveryRun.status, DiscoveryRun.reason_code)
        .all()
    )
    discovery_runs: defaultdict[tuple[str, str], int] = defaultdict(int)
    discovery_failures: defaultdict[tuple[str, str], int] = defaultdict(int)
    discovery_postings: defaultdict[tuple[str, str], int] = defaultdict(int)
    for row in run_rows:
        source = _source_for_run(row.source, lookup)
        result = normalize_discovery_run_result(row.status)
        count = max(0, int(row.run_count or 0))
        discovery_runs[(source, result)] += count
        if result == "failed":
            discovery_failures[(source, normalize_reason_code(row.reason_code))] += count
        for posting_result, value in (
            ("inserted", row.inserted),
            ("updated", row.updated),
            ("duplicate", row.duplicates),
            ("closed", row.closed),
        ):
            discovery_postings[(source, posting_result)] += max(0, int(value or 0))

    latest_fit = (
        db.query(
            JobFitDecisionRecord.job_id.label("job_id"),
            func.max(JobFitDecisionRecord.id).label("latest_id"),
        )
        .group_by(JobFitDecisionRecord.job_id)
        .subquery()
    )
    fit_rows = (
        db.query(
            JobFitDecisionRecord.disposition,
            JobFitDecisionRecord.quality_eligible,
            func.count(JobFitDecisionRecord.id),
        )
        .join(latest_fit, JobFitDecisionRecord.id == latest_fit.c.latest_id)
        .group_by(JobFitDecisionRecord.disposition, JobFitDecisionRecord.quality_eligible)
        .all()
    )
    fit_current: defaultdict[tuple[str, str], int] = defaultdict(int)
    for disposition, eligible, count in fit_rows:
        fit_current[(normalize_fit_disposition(disposition), "true" if eligible else "false")] += (
            max(0, int(count or 0))
        )

    policy_rows = (
        db.query(
            ApplicationPolicyDecision.allowed,
            ApplicationPolicyDecision.reason_codes_json,
            func.count(ApplicationPolicyDecision.id),
        )
        .group_by(
            ApplicationPolicyDecision.allowed,
            ApplicationPolicyDecision.reason_codes_json,
        )
        .all()
    )
    policy_decisions: defaultdict[str, int] = defaultdict(int)
    policy_denials: defaultdict[str, int] = defaultdict(int)
    for allowed, reasons_json, count in policy_rows:
        bounded_count = max(0, int(count or 0))
        decision = "allowed" if allowed else "denied"
        policy_decisions[decision] += bounded_count
        if not allowed:
            for reason in _policy_reasons(reasons_json):
                policy_denials[reason] += bounded_count

    attempt_rows = (
        db.query(
            Submission.adapter_name,
            Submission.submitter_name,
            Submission.outcome,
            func.count(Submission.id),
        )
        .group_by(Submission.adapter_name, Submission.submitter_name, Submission.outcome)
        .all()
    )
    attempts: defaultdict[tuple[str, str], int] = defaultdict(int)
    for adapter_name, submitter_name, outcome, count in attempt_rows:
        ats = normalize_ats(adapter_name or submitter_name)
        attempts[(ats, normalize_outcome(outcome))] += max(0, int(count or 0))

    confirmed: defaultdict[str, int] = defaultdict(int)
    verified_attempts = latest_employer_verified_query(db).with_entities(
        Submission.application_id,
        Submission.adapter_name,
        Submission.submitter_name,
    )
    for _application_id, adapter_name, submitter_name in verified_attempts.all():
        confirmed[normalize_ats(adapter_name or submitter_name)] += 1

    preparation_rows = (
        db.query(
            OperationalMetricRollup.ats,
            OperationalMetricRollup.duration_count,
            OperationalMetricRollup.duration_sum_ms,
            OperationalMetricRollup.duration_le_1s,
            OperationalMetricRollup.duration_le_5s,
            OperationalMetricRollup.duration_le_15s,
            OperationalMetricRollup.duration_le_60s,
            OperationalMetricRollup.duration_le_300s,
            OperationalMetricRollup.duration_le_900s,
            OperationalMetricRollup.duration_le_inf,
        )
        .filter(
            OperationalMetricRollup.metric_name == "attempt_stage",
            OperationalMetricRollup.stage == "preparing",
            OperationalMetricRollup.duration_count > 0,
        )
        .all()
    )
    preparation_latency: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {
            "count": 0,
            "sum_ms": 0,
            "le_1s": 0,
            "le_5s": 0,
            "le_15s": 0,
            "le_60s": 0,
            "le_300s": 0,
            "le_900s": 0,
            "le_inf": 0,
        }
    )
    for row in preparation_rows:
        aggregate = preparation_latency[normalize_ats(row.ats)]
        for target, source in (
            ("count", "duration_count"),
            ("sum_ms", "duration_sum_ms"),
            ("le_1s", "duration_le_1s"),
            ("le_5s", "duration_le_5s"),
            ("le_15s", "duration_le_15s"),
            ("le_60s", "duration_le_60s"),
            ("le_300s", "duration_le_300s"),
            ("le_900s", "duration_le_900s"),
            ("le_inf", "duration_le_inf"),
        ):
            aggregate[target] += max(0, int(getattr(row, source, 0) or 0))

    return {
        "source_instances": dict(source_instances),
        "source_enabled": dict(source_enabled),
        "source_success_available": dict(source_success_available),
        "source_lag": dict(source_lag),
        "discovery_runs": dict(discovery_runs),
        "discovery_failures": dict(discovery_failures),
        "discovery_postings": dict(discovery_postings),
        "fit_current": dict(fit_current),
        "policy_decisions": dict(policy_decisions),
        "policy_denials": dict(policy_denials),
        "attempts": dict(attempts),
        "confirmed": dict(confirmed),
        "preparation_latency": dict(preparation_latency),
    }


class V5OperationalCollector:
    """Prometheus collector derived from authoritative v5 domain records."""

    _registry_marker = "job-agent-v5-operational-v1"

    @staticmethod
    def _new_families():
        from prometheus_client.core import (
            CounterMetricFamily,
            GaugeMetricFamily,
            HistogramMetricFamily,
        )

        return {
            "available": GaugeMetricFamily(
                "job_agent_v5_operational_snapshot_available",
                "Whether the authoritative v5 database snapshot was available.",
            ),
            "source_instances": GaugeMetricFamily(
                "job_agent_discovery_source_instances",
                "Configured discovery source instances by finite source and health state.",
                labels=["source", "status"],
            ),
            "source_lag": GaugeMetricFamily(
                "job_agent_discovery_source_lag_seconds",
                "Worst observed last-success lag among enabled instances of a source type.",
                labels=["source"],
            ),
            "source_success_available": GaugeMetricFamily(
                "job_agent_discovery_source_last_success_available",
                "Whether every enabled source instance has a recorded successful run.",
                labels=["source"],
            ),
            "discovery_runs": CounterMetricFamily(
                "job_agent_discovery_runs",
                "Durable discovery runs by finite source and terminal result.",
                labels=["source", "result"],
            ),
            "discovery_failures": CounterMetricFamily(
                "job_agent_discovery_failures",
                "Failed discovery runs by finite source and stable reason code.",
                labels=["source", "reason_code"],
            ),
            "discovery_postings": CounterMetricFamily(
                "job_agent_discovery_postings",
                "Discovery ingestion outcomes, including deduplication, from durable runs.",
                labels=["source", "result"],
            ),
            "fit_current": GaugeMetricFamily(
                "job_agent_fit_current_jobs",
                "Latest fit decisions per job by disposition and automatic eligibility.",
                labels=["disposition", "auto_eligible"],
            ),
            "fit_qualification_available": GaugeMetricFamily(
                "job_agent_fit_qualification_available",
                "Whether the configured local fit qualification artifact is schema-valid.",
            ),
            "fit_qualification_qualified": GaugeMetricFamily(
                "job_agent_fit_qualification_qualified",
                "Whether the configured local fit qualification passed its release threshold.",
            ),
            "fit_qualification_ratio": GaugeMetricFamily(
                "job_agent_fit_qualification_ratio",
                "Held-out qualification precision, coverage, and abstention.",
                labels=["metric"],
            ),
            "policy_decisions": CounterMetricFamily(
                "job_agent_automation_policy_decisions",
                "Durable signed-policy decisions without application or company labels.",
                labels=["decision"],
            ),
            "policy_denials": CounterMetricFamily(
                "job_agent_automation_policy_denials",
                "Signed-policy denials by stable finite reason code.",
                labels=["reason_code"],
            ),
            "attempts": CounterMetricFamily(
                "job_agent_submission_attempts",
                "Immutable submission attempts by finite ATS and domain outcome.",
                labels=["ats", "outcome"],
            ),
            "confirmed": CounterMetricFamily(
                "job_agent_employer_confirmed_applications",
                "Applications whose latest attempt has exact employer-verified evidence.",
                labels=["ats"],
            ),
            "preparation_latency": HistogramMetricFamily(
                "job_agent_preparation_duration_seconds",
                "Durable submission preparation latency by finite ATS.",
                labels=["ats"],
            ),
        }

    def describe(self):
        try:
            yield from self._new_families().values()
        except ImportError:  # pragma: no cover - dependency-light smoke
            return

    def collect(self):
        try:
            families = self._new_families()
            from db.session import get_session_factory
        except ImportError:  # pragma: no cover - dependency-light smoke
            return

        db = None
        snapshot: dict[str, Any] | None = None
        try:
            db = get_session_factory()()
            snapshot = _database_snapshot(db, now=_naive_utc())
        except Exception:
            if db is not None:
                db.rollback()
        finally:
            if db is not None:
                db.close()

        families["available"].add_metric([], 1 if snapshot is not None else 0)
        qualification = _qualification_snapshot()
        families["fit_qualification_available"].add_metric([], qualification["available"])
        families["fit_qualification_qualified"].add_metric([], qualification["qualified"])
        for metric in QUALIFICATION_RATIO_LABELS:
            if metric in qualification["ratios"]:
                families["fit_qualification_ratio"].add_metric(
                    [metric], qualification["ratios"][metric]
                )

        if snapshot is not None:
            for labels, value in sorted(snapshot["source_instances"].items()):
                families["source_instances"].add_metric(list(labels), value)
            for source in sorted(snapshot["source_enabled"]):
                families["source_lag"].add_metric([source], snapshot["source_lag"].get(source, 0.0))
                all_seen = (
                    snapshot["source_enabled"][source] > 0
                    and snapshot["source_success_available"].get(source, 0)
                    == snapshot["source_enabled"][source]
                )
                families["source_success_available"].add_metric([source], int(all_seen))
            for labels, value in sorted(snapshot["discovery_runs"].items()):
                families["discovery_runs"].add_metric(list(labels), value)
            for labels, value in sorted(snapshot["discovery_failures"].items()):
                families["discovery_failures"].add_metric(list(labels), value)
            for labels, value in sorted(snapshot["discovery_postings"].items()):
                families["discovery_postings"].add_metric(list(labels), value)
            for labels, value in sorted(snapshot["fit_current"].items()):
                families["fit_current"].add_metric(list(labels), value)
            for decision in POLICY_DECISION_LABELS:
                value = snapshot["policy_decisions"].get(decision, 0)
                families["policy_decisions"].add_metric([decision], value)
            for reason, value in sorted(snapshot["policy_denials"].items()):
                families["policy_denials"].add_metric([reason], value)
            for labels, value in sorted(snapshot["attempts"].items()):
                families["attempts"].add_metric(list(labels), value)
            for ats, value in sorted(snapshot["confirmed"].items()):
                families["confirmed"].add_metric([ats], value)
            for ats, aggregate in sorted(snapshot["preparation_latency"].items()):
                families["preparation_latency"].add_metric(
                    [ats],
                    [
                        ("1.0", aggregate["le_1s"]),
                        ("5.0", aggregate["le_5s"]),
                        ("15.0", aggregate["le_15s"]),
                        ("60.0", aggregate["le_60s"]),
                        ("300.0", aggregate["le_300s"]),
                        ("900.0", aggregate["le_900s"]),
                        ("+Inf", aggregate["le_inf"]),
                    ],
                    aggregate["sum_ms"] / 1000.0,
                )

        yield from families.values()


_REGISTRY_COLLECTOR_ATTR = "_job_agent_v5_operational_collector"


def register_v5_operational_collector(registry=None):
    """Register exactly once, including across application hot reloads."""

    if registry is None:
        try:
            from prometheus_client import REGISTRY
        except ImportError:  # pragma: no cover - dependency-light smoke
            return None
        registry = REGISTRY

    existing = getattr(registry, _REGISTRY_COLLECTOR_ATTR, None)
    if existing is not None:
        return existing

    collector = V5OperationalCollector()
    try:
        registry.register(collector)
    except ValueError:
        owners = getattr(registry, "_names_to_collectors", {})
        existing = owners.get("job_agent_v5_operational_snapshot_available")
        if getattr(existing, "_registry_marker", None) != collector._registry_marker:
            raise
        collector = existing
    setattr(registry, _REGISTRY_COLLECTOR_ATTR, collector)
    return collector


__all__ = [
    "V5OperationalCollector",
    "normalize_discovery_source",
    "register_v5_operational_collector",
]
