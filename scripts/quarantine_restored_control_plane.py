"""Quarantine a restored control-plane database before redeployment."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from control_plane.job_control_plane.config import Settings  # noqa: E402
from control_plane.job_control_plane.db import (  # noqa: E402
    build_engine,
    build_session_factory,
)
from control_plane.job_control_plane.restore_safety import (  # noqa: E402
    quarantine_restored_control_plane,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        with factory() as db:
            try:
                summary = quarantine_restored_control_plane(db)
                if args.apply:
                    db.commit()
                else:
                    db.rollback()
            except Exception:
                db.rollback()
                raise
    finally:
        engine.dispose()
    print(
        json.dumps(
            {
                "committed": bool(args.apply),
                "control_plane_restore_quarantine": summary.to_dict(),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
