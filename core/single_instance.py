"""Cross-platform, non-destructive single-instance host/port lock."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import BinaryIO

_LOCAL_BIND_HOSTS = frozenset(
    {
        "0.0.0.0",
        "127.0.0.1",
        "::",
        "::1",
        "[::]",
        "[::1]",
        "localhost",
    }
)
_SERVERLESS_ENV_KEYS = (
    "VERCEL",
    "AWS_LAMBDA_FUNCTION_NAME",
    "FUNCTIONS_WORKER_RUNTIME",
    "SERVERLESS",
)


@dataclass(frozen=True, slots=True)
class DashboardLockConfig:
    """Resolved local dashboard endpoint and lock behavior."""

    enabled: bool
    host: str
    port: int
    lock_dir: Path | None


class AlreadyRunningError(RuntimeError):
    """Raised when another process owns the requested endpoint lock."""

    def __init__(self, host: str, port: int, owner: dict[str, object] | None = None):
        self.host = host
        self.port = port
        self.owner = owner or {}
        pid = self.owner.get("pid", "unknown")
        super().__init__(f"another dashboard process owns {host}:{port} (pid={pid})")


class SingleInstanceLock:
    """Hold an OS-backed advisory lock for one normalized host/port pair.

    The lock file may remain after shutdown, but OS ownership never does.  A
    crashed process therefore cannot leave a false-positive lock, and this
    helper never kills or probes another process.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        boot_id: str = "",
        lock_dir: Path | None = None,
    ) -> None:
        normalized_host = host.strip().lower()
        if not normalized_host or len(normalized_host) > 255:
            raise ValueError("host must be between 1 and 255 characters")
        if any(ord(char) < 32 for char in normalized_host):
            raise ValueError("host cannot contain control characters")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")

        # Loopback and wildcard binds can overlap on Windows while appearing
        # as different textual hosts. Give them one local-machine lock scope
        # so 127.0.0.1:8000 and 0.0.0.0:8000 cannot serve different builds.
        scope_host = "local-machine" if normalized_host in _LOCAL_BIND_HOSTS else normalized_host
        endpoint_hash = hashlib.sha256(f"{scope_host}:{port}".encode()).hexdigest()[:20]
        directory = lock_dir or Path(tempfile.gettempdir()) / "job-apply-agent"
        self.host = normalized_host
        self.port = port
        self.boot_id = boot_id
        self.path = directory / f"dashboard-{endpoint_hash}.lock"
        self._file: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._file is not None

    def acquire(self) -> SingleInstanceLock:
        """Acquire without waiting or modifying another process."""

        if self._file is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+b")
        try:
            self._ensure_lock_byte(lock_file)
            self._lock(lock_file)
        except OSError as exc:
            owner = self._read_owner(lock_file)
            lock_file.close()
            raise AlreadyRunningError(self.host, self.port, owner) from exc

        metadata = {
            "pid": os.getpid(),
            "host": self.host,
            "port": self.port,
            "boot_id": self.boot_id,
            "acquired_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(b"\0" + json.dumps(metadata, separators=(",", ":")).encode("utf-8"))
        lock_file.flush()
        os.fsync(lock_file.fileno())
        self._file = lock_file
        return self

    def release(self) -> None:
        """Release this process's lock; the metadata file remains harmless."""

        if self._file is None:
            return
        lock_file = self._file
        self._file = None
        try:
            self._unlock(lock_file)
        finally:
            lock_file.close()

    @staticmethod
    def _ensure_lock_byte(lock_file: BinaryIO) -> None:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()

    @staticmethod
    def _read_owner(lock_file: BinaryIO) -> dict[str, object] | None:
        try:
            # Byte zero is the locked region.  Keeping metadata after it lets
            # another Windows process read the bounded owner information
            # without attempting to read the locked byte itself.
            lock_file.seek(1)
            raw = lock_file.read(4096).decode("utf-8").strip()
            parsed = json.loads(raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None
        allowed = {"pid", "host", "port", "boot_id", "acquired_at"}
        return {key: value for key, value in parsed.items() if key in allowed}

    @staticmethod
    def _lock(lock_file: BinaryIO) -> None:
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(lock_file: BinaryIO) -> None:
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> SingleInstanceLock:
        return self.acquire()

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


def _cli_option(argv: Sequence[str], name: str) -> str | None:
    prefix = f"{name}="
    for index, argument in enumerate(argv):
        if argument.startswith(prefix):
            return argument[len(prefix) :]
        if argument == name and index + 1 < len(argv):
            return argv[index + 1]
    return None


def resolve_dashboard_lock_config(
    *,
    app_env: str,
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> DashboardLockConfig:
    """Resolve the endpoint without coupling normal ASGI imports to Uvicorn."""

    env = os.environ if environ is None else environ
    arguments = sys.argv[1:] if argv is None else argv
    host = (
        env.get("JOB_AGENT_API_HOST") or _cli_option(arguments, "--host") or "127.0.0.1"
    ).strip()
    raw_port = (
        env.get("JOB_AGENT_API_PORT")
        or env.get("PORT")
        or _cli_option(arguments, "--port")
        or "8000"
    )
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("dashboard port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("dashboard port must be between 1 and 65535")

    explicit = str(env.get("JOB_AGENT_INSTANCE_LOCK", "")).strip().lower()
    explicitly_disabled = explicit in {"0", "false", "no", "off"}
    serverless = any(str(env.get(key, "")).strip() for key in _SERVERLESS_ENV_KEYS)
    under_pytest = bool(str(env.get("PYTEST_CURRENT_TEST", "")).strip())
    enabled = not explicitly_disabled and app_env != "test" and not serverless and not under_pytest
    raw_lock_dir = str(env.get("JOB_AGENT_INSTANCE_LOCK_DIR", "")).strip()
    lock_dir = Path(raw_lock_dir) if raw_lock_dir else None
    return DashboardLockConfig(
        enabled=enabled,
        host=host,
        port=port,
        lock_dir=lock_dir,
    )


def acquire_dashboard_instance_lock(
    *,
    app_env: str,
    boot_id: str,
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> SingleInstanceLock | None:
    """Acquire the configured dashboard lock, or skip serverless/test runtimes."""

    config = resolve_dashboard_lock_config(
        app_env=app_env,
        environ=environ,
        argv=argv,
    )
    if not config.enabled:
        return None
    return SingleInstanceLock(
        config.host,
        config.port,
        boot_id=boot_id,
        lock_dir=config.lock_dir,
    ).acquire()
