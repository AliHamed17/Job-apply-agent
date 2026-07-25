"""Canonical local dashboard launcher with endpoint identity locking."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import uvicorn


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Start the Job Apply Agent dashboard")
    parser.add_argument(
        "--host",
        default=os.environ.get("JOB_AGENT_API_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=os.environ.get("JOB_AGENT_API_PORT", "8000"),
    )
    parser.add_argument("--reload", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """Start Uvicorn and make its endpoint visible to the lifespan lock."""

    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("port must be between 1 and 65535")
    os.environ["JOB_AGENT_API_HOST"] = args.host
    os.environ["JOB_AGENT_API_PORT"] = str(args.port)
    os.environ["JOB_AGENT_INSTANCE_LOCK"] = "true"
    project_root = str(Path(__file__).resolve().parents[1])
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    uvicorn.run(
        "api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
