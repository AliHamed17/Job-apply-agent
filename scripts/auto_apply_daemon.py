"""Deprecated alias for the real discovery-and-draft automation runner.

This command never inserts sample jobs and never marks database rows submitted.
"""

from scripts.run_safe_automation import main

if __name__ == "__main__":
    raise SystemExit(main())
