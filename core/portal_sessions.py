"""Dedicated browser-session profiles for authenticated employer portals.

The application never reads Chrome/Edge password stores. An operator signs in
once in a dedicated Playwright profile, and later workers reuse only that
portal's cookies and browser state.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_READY_MARKER = ".job-agent-session-ready"


class PortalSessionError(RuntimeError):
    """Raised when a portal session cannot be resolved or safely leased."""


def _process_is_running(pid: int) -> bool:
    """Check a lock owner without sending a signal that could stop it."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return False
        kernel32 = win_dll("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        try:
            return (
                bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)))
                and exit_code.value == still_active
            )
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _hostname(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise PortalSessionError("A valid HTTP/HTTPS employer URL is required.")
    return host


def _profile_key(hostname: str) -> str:
    key = re.sub(r"[^a-z0-9.-]+", "-", hostname.lower()).strip(".-")
    if not key or key in {".", ".."}:
        raise PortalSessionError("The employer hostname cannot be used as a profile key.")
    return key[:180]


@dataclass(frozen=True)
class PortalSession:
    hostname: str
    profile_key: str
    profile_dir: Path

    @property
    def ready_marker(self) -> Path:
        return self.profile_dir / _READY_MARKER

    @property
    def ready(self) -> bool:
        """Whether browser state and explicit bootstrap confirmation exist."""
        if not self.profile_dir.is_dir() or not self.ready_marker.is_file():
            return False
        try:
            return any(path != self.ready_marker for path in self.profile_dir.iterdir())
        except OSError:
            return False

    def mark_ready(self) -> None:
        """Record explicit operator confirmation without storing credentials."""
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.ready_marker.write_text("version=1\n", encoding="ascii")


def portal_session_for_url(url: str, profile_root: str | Path) -> PortalSession:
    """Map an employer URL to a tenant-isolated browser profile."""
    host = _hostname(url)
    root = Path(profile_root).expanduser().resolve()
    key = _profile_key(host)
    profile_dir = (root / key).resolve()
    if profile_dir.parent != root:
        raise PortalSessionError("Resolved portal profile escaped its configured root.")
    return PortalSession(hostname=host, profile_key=key, profile_dir=profile_dir)


class PortalSessionLease:
    """Cross-process best-effort lease for a persistent browser profile."""

    def __init__(self, session: PortalSession, stale_minutes: int = 30):
        self.session = session
        self.stale_seconds = max(1, stale_minutes) * 60
        self.lock_path = session.profile_dir.parent / f".{session.profile_key}.lock"
        self._held = False

    def acquire(self) -> None:
        self.session.profile_dir.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                descriptor = os.open(
                    self.lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                except OSError:
                    age = 0
                if age <= self.stale_seconds:
                    raise PortalSessionError("PORTAL_SESSION_BUSY") from None
                try:
                    owner_pid = int(self.lock_path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    owner_pid = 0
                if _process_is_running(owner_pid):
                    raise PortalSessionError("PORTAL_SESSION_BUSY") from None
                try:
                    self.lock_path.unlink()
                except OSError as exc:
                    raise PortalSessionError("PORTAL_SESSION_BUSY") from exc
                continue
            with os.fdopen(descriptor, "w", encoding="ascii") as handle:
                handle.write(f"{os.getpid()}\n")
            self._held = True
            return
        raise PortalSessionError("PORTAL_SESSION_BUSY")

    def release(self) -> None:
        if not self._held:
            return
        try:
            self.lock_path.unlink(missing_ok=True)
        finally:
            self._held = False

    def __enter__(self) -> PortalSessionLease:
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
