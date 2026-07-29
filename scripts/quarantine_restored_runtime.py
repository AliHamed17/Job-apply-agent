"""Quarantine restored private runtime authority before any worker starts."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.restore_safety import quarantine_restored_runtime  # noqa: E402
from db.session import get_session_factory  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--apply",
        action="store_true",
        help="commit quarantine changes to DATABASE_URL",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="calculate counts and roll the transaction back",
    )
    args = parser.parse_args(argv)

    factory = get_session_factory()
    with factory() as db:
        try:
            summary = quarantine_restored_runtime(db)
            if args.apply:
                db.commit()
            else:
                db.rollback()
        except Exception:
            db.rollback()
            raise
    result: dict[str, object] = {
        "committed": bool(args.apply),
        "restore_quarantine": summary.to_dict(),
    }
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
