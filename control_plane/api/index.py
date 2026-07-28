"""Vercel Python Function entrypoint."""

from __future__ import annotations

import sys
from pathlib import Path

CONTROL_PLANE_ROOT = Path(__file__).resolve().parents[1]
if str(CONTROL_PLANE_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTROL_PLANE_ROOT))

from job_control_plane.app import create_app  # noqa: E402

app = create_app()
