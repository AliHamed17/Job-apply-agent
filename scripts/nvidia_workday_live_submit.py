"""Deprecated name retained as a safe Workday session-bootstrap entry point."""

from scripts.portal_session_bootstrap import main

if __name__ == "__main__":
    raise SystemExit(main())
