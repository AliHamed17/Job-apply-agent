"""Guarded one-URL LinkedIn Easy Apply dry-run qualification."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from profile.loader import get_profile  # noqa: E402

from core.config import get_settings  # noqa: E402
from db.models import BrowserQualificationRun  # noqa: E402
from db.session import get_session_factory, init_db  # noqa: E402
from jobs.models import JobData  # noqa: E402
from llm.generation import GeneratedApplication  # noqa: E402
from submitters.browser_trace import RedactedTrace  # noqa: E402
from submitters.linkedin_v2 import LinkedInV2Submitter  # noqa: E402


def validate_smoke_guard(url: str, token: str | None = None) -> None:
    settings = get_settings()
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in {
        "www.linkedin.com",
        "linkedin.com",
    } or not parsed.path.startswith("/jobs/"):
        raise RuntimeError("Exactly one explicit HTTPS LinkedIn job URL is required.")
    if not settings.dry_run:
        raise RuntimeError("Refusing smoke test: DRY_RUN=true is required.")
    if settings.secret_key in {"", "change-me", "change-me-to-a-random-secret"}:
        raise RuntimeError("Refusing smoke test: configure a non-default operator secret.")
    supplied = token or os.environ.get("JOB_AGENT_OPERATOR_TOKEN", "")
    if not supplied or not hmac.compare_digest(supplied, settings.secret_key):
        raise RuntimeError("Refusing smoke test: operator authentication failed.")


async def run_smoke(url: str, report_path: str) -> int:
    validate_smoke_guard(url)
    profile = get_profile()
    trace = RedactedTrace()
    submitter = LinkedInV2Submitter(trace=trace)
    result = await submitter.submit(
        JobData(title="Dry-run qualification", apply_url=url, source_url=url),
        GeneratedApplication(),
        profile.model_dump(),
        profile.resume.pdf_path or None,
    )
    qualified = (
        result.status == "draft_only"
        and result.error == "DRY_RUN"
        and any(
            event.get("terminal_reason") == "DRY_RUN_DISCARDED"
            for event in trace.events
        )
    )
    trace.write_report(report_path, qualified=qualified)
    terminal = next(
        (
            event.get("terminal_reason")
            for event in reversed(trace.events)
            if event.get("terminal_reason")
        ),
        "UNKNOWN",
    )
    init_db()
    db = get_session_factory()()
    try:
        db.add(
            BrowserQualificationRun(
                selector_version=(
                    trace.events[-1]["selector_version"]
                    if trace.events
                    else "unknown"
                ),
                terminal_reason=terminal,
                qualified=qualified,
                trace_json=json.dumps(trace.events, separators=(",", ":")),
            )
        )
        db.commit()
    finally:
        db.close()
    return 0 if qualified else 2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--report", default="linkedin-smoke-qualification.json")
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run_smoke(args.url, args.report)))
    except RuntimeError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
