"""Provision versioned control-plane identities outside the repository.

The command emits only public identifiers and paths. Private control-plane
material is protected with Windows DPAPI; the runner private key is stored in
an ACL-restricted file because the outbound runner must read it directly.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

CONTROL_AUDIENCE = "job-apply-control-plane"
RUNNER_AUDIENCE = "job-apply-private-runner"
DPAPI_DESCRIPTION = "Job Apply Agent control-plane secrets"
DPAPI_ENTROPY = b"JobApplyAgent/control-plane/v1"
MAX_SECRET_BUNDLE_BYTES = 32 * 1024
_REPARSE_POINT = 0x400
_FILE_SHARE_ALL = 0x00000007
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_ERROR_NOT_A_REPARSE_POINT = 4390
_MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024
_DRIVE_FIXED = 3
_MIN_CLIPBOARD_TTL_SECONDS = 15
_MAX_CLIPBOARD_TTL_SECONDS = 300
_CLIPBOARD_CLEAR_ATTEMPTS = 5
_CLIPBOARD_CLEAR_RETRY_SECONDS = 0.1
_IDENTITY_PUBLISH_ATTEMPTS = 5
_IDENTITY_PUBLISH_RETRY_SECONDS = 0.05
_TRANSIENT_IDENTITY_PUBLISH_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
_TRANSIENT_IDENTITY_PUBLISH_WINERRORS = frozenset({5, 32, 33})
_IDENTITY_ATTESTATION_CONTEXT = b"JobApplyAgent/control-identity-bundle/v2\0"
_MAX_VERCEL_METADATA_BYTES = 2 * 1024 * 1024
_MAX_VERCEL_CA_BYTES = 1024 * 1024
_VERCEL_ENV_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
_IDENTITY_DERIVED_VERCEL_VARIABLES = (
    "CONTROL_OPERATOR_TOKEN",
    "CONTROL_SESSION_SECRET",
    "CONTROL_CSRF_SECRET",
    "CONTROL_SIGNING_PRIVATE_KEY_B64",
    "CONTROL_SIGNING_KEY_ID",
    "CONTROL_RUNNER_PUBLIC_KEY_B64",
    "CONTROL_RUNNER_DEVICE_ID",
    "CONTROL_IDENTITY_BUNDLE_DIGEST",
)
_CURRENT_KEYS = frozenset({"schema_version", "version_id", "bundle_path"})
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "version_id",
        "created_at",
        "device_id",
        "device_public_key",
        "control_signing_key_id",
        "control_public_key",
        "control_audience",
        "runner_audience",
        "control_plane_url",
        "vercel_environment",
        "vercel_project_id",
        "vercel_scope_id",
        "runner_config_path",
        "secret_bundle_path",
    }
)
_PROTECTED_IDENTITY_KEYS = frozenset(
    {
        "schema_version",
        "version_id",
        "control_plane_url",
        "vercel_environment",
        "vercel_project_id",
        "vercel_scope_id",
        "control_private_key",
        "operator_token",
        "session_secret",
        "csrf_secret",
    }
)
_RUNNER_CONFIG_KEYS = frozenset(
    {
        "control_plane_url",
        "device_id",
        "control_signing_key_id",
        "control_plane_audience",
        "private_key_path",
        "control_plane_public_key_path",
        "runtime_env_path",
        "poll_interval_seconds",
        "heartbeat_interval_seconds",
        "offline_after_seconds",
    }
)


class IdentityProvisioningError(RuntimeError):
    """A stable provisioning failure that never includes secret material."""


def _windows_dll(name: str) -> Any:
    """Resolve a Windows DLL without requiring Windows-only ctypes stubs."""

    loader = getattr(ctypes, "windll", None)
    library = getattr(loader, name, None)
    if library is None:
        raise IdentityProvisioningError("WINDOWS_API_UNAVAILABLE")
    return library


class ClipboardController(Protocol):
    """Minimal native clipboard boundary used by the explicit copy command."""

    def set_text(self, value: str) -> ClipboardLease: ...

    def clear_if_unchanged(self, expected: str, lease: ClipboardLease) -> bool: ...


@dataclass(frozen=True, slots=True)
class ClipboardLease:
    """Sequence-bound ownership proof for one clipboard write."""

    sequence_number: int


@dataclass(frozen=True, slots=True)
class SelectedIdentity:
    """Strictly validated public selection plus the protected bundle path."""

    root: Path
    bundle_path: Path
    version_id: UUID
    device_id: UUID
    control_signing_key_id: UUID
    runner_public_key: str
    control_public_key: str
    secret_bundle_path: Path
    control_plane_url: str
    vercel_environment: str
    vercel_project_id: str
    vercel_scope_id: str


@dataclass(frozen=True, slots=True)
class VercelConfigurationResult:
    """Non-secret result safe to serialize to stdout."""

    dry_run: bool
    cli_mode: str
    environment: str
    project: str
    scope: str
    variable_names: tuple[str, ...]
    configured_count: int
    expected_cli_version: str


@dataclass(frozen=True, slots=True)
class VercelCliInvocation:
    """Exact executable, argument prefix, and file pins for one CLI mode."""

    mode: str
    executable: Path
    prefix_arguments: tuple[str, ...]
    native_sha256: str | None = None
    node_sha256: str | None = None
    js_entrypoint: Path | None = None
    js_entrypoint_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class VercelCaTrust:
    """Explicitly pinned public CA bundle for one Vercel CLI flow."""

    certificate: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class VercelEnvironmentInventory:
    """Non-decrypted exact-target identity record inventory."""

    targeted_records: tuple[tuple[str, str], ...]
    other_target_records: tuple[tuple[str, str, str], ...]


VercelCommandRunner = Callable[
    [Path, Sequence[str], str, Path, Mapping[str, str]],
    int,
]
VercelMetadataReader = Callable[
    [Path, Sequence[str], Path, Mapping[str, str]],
    str,
]
VercelVersionReader = Callable[[Path, Path, Mapping[str, str]], str]


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _blob(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    return (
        _DataBlob(
            len(value),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)),
        ),
        buffer,
    )


def protect_with_dpapi(value: bytes) -> bytes:
    """Protect bytes for the current Windows user without printing them."""

    if os.name != "nt":
        raise IdentityProvisioningError("DPAPI_UNAVAILABLE")
    crypt32 = _windows_dll("crypt32")
    kernel32 = _windows_dll("kernel32")
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, source_buffer = _blob(value)
    entropy, entropy_buffer = _blob(DPAPI_ENTROPY)
    protected = _DataBlob()
    # Keep buffers alive for the native call.
    _ = (source_buffer, entropy_buffer)
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        DPAPI_DESCRIPTION,
        ctypes.byref(entropy),
        None,
        None,
        0x1,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(protected),
    )
    if not ok:
        raise IdentityProvisioningError("DPAPI_PROTECT_FAILED")
    try:
        return ctypes.string_at(protected.pbData, protected.cbData)
    finally:
        kernel32.LocalFree(protected.pbData)


def unprotect_with_dpapi(value: bytes) -> bytes:
    """Unprotect a bundle for an in-process deployment flow."""

    if os.name != "nt":
        raise IdentityProvisioningError("DPAPI_UNAVAILABLE")
    crypt32 = _windows_dll("crypt32")
    kernel32 = _windows_dll("kernel32")
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    source, source_buffer = _blob(value)
    entropy, entropy_buffer = _blob(DPAPI_ENTROPY)
    plaintext = _DataBlob()
    description = wintypes.LPWSTR()
    _ = (source_buffer, entropy_buffer)
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        ctypes.byref(entropy),
        None,
        None,
        0x1,
        ctypes.byref(plaintext),
    )
    if not ok:
        raise IdentityProvisioningError("DPAPI_UNPROTECT_FAILED")
    try:
        result = ctypes.string_at(plaintext.pbData, plaintext.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(plaintext.pbData)
    if len(result) > MAX_SECRET_BUNDLE_BYTES:
        raise IdentityProvisioningError("SECRET_BUNDLE_INVALID")
    return result


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _private_text(key: Ed25519PrivateKey) -> str:
    return _b64url(
        key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _public_text(key: Ed25519PrivateKey) -> str:
    return _b64url(
        key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def _decode_base64url_key(value: str, *, code: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise IdentityProvisioningError(code)
    try:
        raw = base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
    except (ValueError, TypeError) as exc:
        raise IdentityProvisioningError(code) from exc
    if len(raw) != 32 or _b64url(raw) != value:
        raise IdentityProvisioningError(code)
    return raw


def _canonical_uuid(value: object, *, code: str) -> UUID:
    try:
        parsed = UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise IdentityProvisioningError(code) from exc
    if str(parsed) != str(value):
        raise IdentityProvisioningError(code)
    return parsed


def _exact_vercel_id(value: object, *, prefix: str, code: str) -> str:
    cleaned = str(value)
    if not re.fullmatch(rf"{re.escape(prefix)}[A-Za-z0-9]{{8,120}}", cleaned) or cleaned != str(
        value
    ):
        raise IdentityProvisioningError(code)
    return cleaned


def _exact_vercel_environment(value: object, *, code: str) -> str:
    cleaned = str(value)
    if cleaned not in {"production", "preview"}:
        raise IdentityProvisioningError(code)
    return cleaned


def _exact_cli_digest(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise IdentityProvisioningError("VERCEL_CLI_DIGEST_INVALID")
    return value


def _exact_cli_version(value: str) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", value):
        raise IdentityProvisioningError("VERCEL_CLI_VERSION_INVALID")
    return value


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_strict_json_object(
    path: Path,
    *,
    expected_keys: frozenset[str],
    maximum_bytes: int = 32 * 1024,
    code: str,
) -> dict[str, object]:
    if _has_reparse_ancestor(path):
        raise IdentityProvisioningError(code)
    try:
        size = path.stat().st_size
        raw = path.read_bytes()
    except OSError as exc:
        raise IdentityProvisioningError(code) from exc
    if not path.is_file() or not 0 < size <= maximum_bytes or len(raw) != size:
        raise IdentityProvisioningError(code)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise IdentityProvisioningError(code) from exc
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise IdentityProvisioningError(code)
    return value


def _read_bounded_json_object(
    path: Path,
    *,
    required_keys: frozenset[str],
    maximum_bytes: int,
    code: str,
) -> dict[str, object]:
    """Read a bounded object while allowing provider-owned additional keys."""

    if _has_reparse_ancestor(path):
        raise IdentityProvisioningError(code)
    try:
        size = path.stat().st_size
        raw = path.read_bytes()
    except OSError as exc:
        raise IdentityProvisioningError(code) from exc
    if not path.is_file() or not 0 < size <= maximum_bytes or len(raw) != size:
        raise IdentityProvisioningError(code)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise IdentityProvisioningError(code) from exc
    if not isinstance(value, dict) or not required_keys.issubset(value):
        raise IdentityProvisioningError(code)
    return value


def _read_public_key_file(path: Path, *, code: str) -> str:
    if _has_reparse_ancestor(path):
        raise IdentityProvisioningError(code)
    try:
        size = path.stat().st_size
        raw = path.read_bytes()
    except OSError as exc:
        raise IdentityProvisioningError(code) from exc
    if not path.is_file() or not 32 <= size <= 128 or len(raw) != size:
        raise IdentityProvisioningError(code)
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise IdentityProvisioningError(code) from exc
    if "\r" in value or "\n" in value:
        raise IdentityProvisioningError(code)
    _decode_base64url_key(value, code=code)
    return value


def _default_root() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        raise IdentityProvisioningError("LOCALAPPDATA_UNAVAILABLE")
    return Path(local_app_data) / "JobApplyAgent" / "control-plane"


def _is_unc(path: Path) -> bool:
    value = str(path)
    return value.startswith(("\\\\", "//"))


def _is_local_fixed_ntfs_path(path: Path) -> bool:
    """Reject mapped/network/removable volumes at every private execution boundary."""

    if os.name != "nt":
        return True
    anchor = path.anchor
    if not anchor or _is_unc(Path(anchor)):
        return False
    kernel32 = _windows_dll("kernel32")
    kernel32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetDriveTypeW.restype = wintypes.UINT
    if int(kernel32.GetDriveTypeW(anchor)) != _DRIVE_FIXED:
        return False
    kernel32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    kernel32.GetVolumeInformationW.restype = wintypes.BOOL
    filesystem = ctypes.create_unicode_buffer(32)
    serial = wintypes.DWORD()
    maximum_component = wintypes.DWORD()
    flags = wintypes.DWORD()
    if not kernel32.GetVolumeInformationW(
        anchor,
        None,
        0,
        ctypes.byref(serial),
        ctypes.byref(maximum_component),
        ctypes.byref(flags),
        filesystem,
        len(filesystem),
    ):
        return False
    return filesystem.value.casefold() == "ntfs"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
    except ValueError:
        return False
    return True


def _normalized_absolute(path: Path) -> Path:
    """Normalize path syntax without following a junction or symlink."""

    return Path(os.path.abspath(os.fspath(path)))


def _known_onedrive_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        raw = os.environ.get(name, "").strip()
        if raw:
            roots.append(Path(raw))
    return tuple(roots)


def _is_onedrive_path(path: Path) -> bool:
    raw = _normalized_absolute(path)
    resolved = raw.resolve(strict=False)
    roots = tuple(
        candidate
        for root_path in _known_onedrive_roots()
        for candidate in (
            _normalized_absolute(root_path),
            root_path.resolve(strict=False),
        )
    )
    return any(
        _is_within(candidate, root) for candidate in (raw, resolved) for root in roots
    ) or any(
        part.casefold().startswith("onedrive")
        for candidate in (raw, resolved)
        for part in candidate.parts
    )


def _windows_path_is_reparse_point(path: Path) -> bool:
    """Probe the reparse tag through a no-follow handle, including cloud tags."""

    if os.name != "nt":
        return False
    kernel32 = _windows_dll("kernel32")
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    handle = kernel32.CreateFileW(
        str(path),
        0,
        _FILE_SHARE_ALL,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle in (None, invalid_handle):
        return True
    try:
        buffer = ctypes.create_string_buffer(_MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
        returned = wintypes.DWORD()
        if kernel32.DeviceIoControl(
            handle,
            _FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buffer,
            len(buffer),
            ctypes.byref(returned),
            None,
        ):
            return True
        return int(kernel32.GetLastError()) != _ERROR_NOT_A_REPARSE_POINT
    finally:
        kernel32.CloseHandle(handle)


def _has_reparse_ancestor(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current.exists():
            attributes = getattr(current.lstat(), "st_file_attributes", 0)
            if attributes & _REPARSE_POINT or _windows_path_is_reparse_point(current):
                return True
        if current.parent == current:
            return False
        current = current.parent


def validate_external_root(root: Path, *, repository_root: Path) -> Path:
    """Reject repositories, synced folders, UNC paths, and reparse points."""

    if not root.is_absolute() or _is_unc(root) or not _is_local_fixed_ntfs_path(root):
        raise IdentityProvisioningError("IDENTITY_ROOT_NOT_LOCAL_ABSOLUTE")
    raw = _normalized_absolute(root)
    if _has_reparse_ancestor(raw):
        # Inspect the caller's path before resolving it; resolving first erases
        # the junction/symlink evidence this boundary is meant to reject.
        raise IdentityProvisioningError("IDENTITY_ROOT_REPARSE_POINT")
    resolved = raw.resolve(strict=False)
    if _is_unc(resolved):
        raise IdentityProvisioningError("IDENTITY_ROOT_NOT_LOCAL_ABSOLUTE")
    repository = repository_root.resolve(strict=True)
    if any(
        _is_within(candidate, repository) or _is_within(repository, candidate)
        for candidate in (raw, resolved)
    ):
        raise IdentityProvisioningError("IDENTITY_ROOT_IN_REPOSITORY")
    if _is_onedrive_path(raw):
        raise IdentityProvisioningError("IDENTITY_ROOT_IN_ONEDRIVE")
    if _has_reparse_ancestor(resolved):
        raise IdentityProvisioningError("IDENTITY_ROOT_REPARSE_POINT")
    return resolved


def _write_new(path: Path, value: bytes, *, private: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    try:
        try:
            remaining = memoryview(value)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError(errno.EIO, "identity write made no progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    if private:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def _write_json_new(path: Path, value: Mapping[str, object]) -> None:
    encoded = json.dumps(
        dict(value),
        ensure_ascii=True,
        allow_nan=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _write_new(path, encoded + b"\n", private=False)


def _tighten_windows_acl(path: Path) -> None:
    if os.name != "nt":
        return
    username = os.environ.get("USERNAME", "").strip()
    userdomain = os.environ.get("USERDOMAIN", "").strip()
    principal = f"{userdomain}\\{username}" if userdomain and username else username
    if not principal:
        raise IdentityProvisioningError("WINDOWS_IDENTITY_UNAVAILABLE")
    result = subprocess.run(
        [
            "icacls.exe",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{principal}:(OI)(CI)F",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise IdentityProvisioningError("IDENTITY_ACL_FAILED")


def _publish_identity_version(staging: Path, final: Path) -> None:
    """Atomically publish one version after brief Windows ACL/share contention."""

    for attempt in range(_IDENTITY_PUBLISH_ATTEMPTS):
        if final.exists():
            raise IdentityProvisioningError("IDENTITY_VERSION_EXISTS")
        try:
            os.replace(staging, final)
            return
        except PermissionError as exc:
            # Never retry into a path that appeared after the initial
            # existence check. This preserves the immutable/no-overwrite
            # contract if another publisher wins the race.
            if final.exists():
                raise IdentityProvisioningError("IDENTITY_VERSION_EXISTS") from exc
            winerror = getattr(exc, "winerror", None)
            transient = (
                exc.errno in _TRANSIENT_IDENTITY_PUBLISH_ERRNOS
                or winerror in _TRANSIENT_IDENTITY_PUBLISH_WINERRORS
            )
            if not transient or attempt + 1 == _IDENTITY_PUBLISH_ATTEMPTS or not staging.is_dir():
                raise
            time.sleep(_IDENTITY_PUBLISH_RETRY_SECONDS)


def _validate_control_plane_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise IdentityProvisioningError("CONTROL_PLANE_URL_INVALID") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise IdentityProvisioningError("CONTROL_PLANE_URL_INVALID")
    return value.rstrip("/")


def load_selected_identity(
    *,
    root: Path,
    repository_root: Path,
) -> SelectedIdentity:
    """Load only a strict, externally stored current identity selection."""

    safe_root = validate_external_root(root, repository_root=repository_root)
    current_path = safe_root / "current.json"
    current = _read_strict_json_object(
        current_path,
        expected_keys=_CURRENT_KEYS,
        code="IDENTITY_SELECTION_INVALID",
    )
    if current["schema_version"] != 2:
        raise IdentityProvisioningError("IDENTITY_SELECTION_INVALID")
    version_id = _canonical_uuid(
        current["version_id"],
        code="IDENTITY_SELECTION_INVALID",
    )
    expected_bundle = safe_root / "versions" / str(version_id)
    supplied_bundle = Path(str(current["bundle_path"]))
    if not supplied_bundle.is_absolute():
        raise IdentityProvisioningError("IDENTITY_SELECTION_INVALID")
    if _has_reparse_ancestor(supplied_bundle):
        raise IdentityProvisioningError("IDENTITY_SELECTION_REPARSE_POINT")
    try:
        bundle = supplied_bundle.resolve(strict=True)
        expected = expected_bundle.resolve(strict=True)
    except OSError as exc:
        raise IdentityProvisioningError("IDENTITY_SELECTION_INVALID") from exc
    if bundle != expected or not bundle.is_dir() or not _is_within(bundle, safe_root):
        raise IdentityProvisioningError("IDENTITY_SELECTION_INVALID")

    manifest = _read_strict_json_object(
        bundle / "manifest.json",
        expected_keys=_MANIFEST_KEYS,
        code="IDENTITY_MANIFEST_INVALID",
    )
    if manifest["schema_version"] != 2 or str(manifest["version_id"]) != str(version_id):
        raise IdentityProvisioningError("IDENTITY_MANIFEST_INVALID")
    device_id = _canonical_uuid(
        manifest["device_id"],
        code="IDENTITY_MANIFEST_INVALID",
    )
    control_signing_key_id = _canonical_uuid(
        manifest["control_signing_key_id"],
        code="IDENTITY_MANIFEST_INVALID",
    )
    if device_id == control_signing_key_id:
        raise IdentityProvisioningError("IDENTITY_MANIFEST_INVALID")
    if (
        manifest["control_audience"] != CONTROL_AUDIENCE
        or manifest["runner_audience"] != RUNNER_AUDIENCE
    ):
        raise IdentityProvisioningError("IDENTITY_MANIFEST_INVALID")
    endpoint = _validate_control_plane_url(str(manifest["control_plane_url"]))
    vercel_environment = _exact_vercel_environment(
        manifest["vercel_environment"],
        code="IDENTITY_MANIFEST_INVALID",
    )
    vercel_project_id = _exact_vercel_id(
        manifest["vercel_project_id"],
        prefix="prj_",
        code="IDENTITY_MANIFEST_INVALID",
    )
    vercel_scope_id = _exact_vercel_id(
        manifest["vercel_scope_id"],
        prefix="team_",
        code="IDENTITY_MANIFEST_INVALID",
    )

    runner_config_path = bundle / "runner.json"
    secret_bundle_path = bundle / "control-secrets.dpapi"
    if (
        Path(str(manifest["runner_config_path"])) != runner_config_path
        or Path(str(manifest["secret_bundle_path"])) != secret_bundle_path
    ):
        raise IdentityProvisioningError("IDENTITY_MANIFEST_INVALID")

    runner_public_key = _read_public_key_file(
        bundle / "runner-public.key",
        code="RUNNER_PUBLIC_KEY_INVALID",
    )
    control_public_key = _read_public_key_file(
        bundle / "control-public.key",
        code="CONTROL_PUBLIC_KEY_INVALID",
    )
    if (
        str(manifest["device_public_key"]) != runner_public_key
        or str(manifest["control_public_key"]) != control_public_key
        or secrets.compare_digest(runner_public_key, control_public_key)
    ):
        raise IdentityProvisioningError("IDENTITY_MANIFEST_INVALID")

    runner = _read_strict_json_object(
        runner_config_path,
        expected_keys=_RUNNER_CONFIG_KEYS,
        code="RUNNER_CONFIG_INVALID",
    )
    if (
        runner["control_plane_url"] != endpoint
        or runner["device_id"] != str(device_id)
        or runner["control_signing_key_id"] != str(control_signing_key_id)
        or runner["control_plane_audience"] != CONTROL_AUDIENCE
        or runner["poll_interval_seconds"] != 10
        or runner["heartbeat_interval_seconds"] != 10
        or runner["offline_after_seconds"] != 30
    ):
        raise IdentityProvisioningError("RUNNER_CONFIG_INVALID")
    runner_private_path = bundle / "runner-private.key"
    control_public_path = bundle / "control-public.key"
    if (
        Path(str(runner["private_key_path"])) != runner_private_path
        or Path(str(runner["control_plane_public_key_path"])) != control_public_path
    ):
        raise IdentityProvisioningError("RUNNER_CONFIG_INVALID")
    runtime_env_path = Path(str(runner["runtime_env_path"]))
    if not runtime_env_path.is_absolute():
        raise IdentityProvisioningError("RUNNER_ENV_PATH_NOT_ABSOLUTE")
    try:
        validate_external_root(
            runtime_env_path.parent,
            repository_root=repository_root,
        )
    except IdentityProvisioningError as exc:
        raise IdentityProvisioningError("RUNNER_ENV_PATH_NOT_EXTERNAL") from exc

    for path, code, minimum, maximum in (
        (runner_private_path, "RUNNER_PRIVATE_KEY_INVALID", 32, 16 * 1024),
        (secret_bundle_path, "SECRET_BUNDLE_INVALID", 1, MAX_SECRET_BUNDLE_BYTES),
    ):
        if _has_reparse_ancestor(path):
            raise IdentityProvisioningError(code)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise IdentityProvisioningError(code) from exc
        if not path.is_file() or not minimum <= size <= maximum:
            raise IdentityProvisioningError(code)

    try:
        runner_private_text = runner_private_path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise IdentityProvisioningError("RUNNER_PRIVATE_KEY_INVALID") from exc
    if "\r" in runner_private_text or "\n" in runner_private_text:
        raise IdentityProvisioningError("RUNNER_PRIVATE_KEY_INVALID")
    try:
        runner_private = Ed25519PrivateKey.from_private_bytes(
            _decode_base64url_key(
                runner_private_text,
                code="RUNNER_PRIVATE_KEY_INVALID",
            )
        )
    except ValueError as exc:
        raise IdentityProvisioningError("RUNNER_PRIVATE_KEY_INVALID") from exc
    if not secrets.compare_digest(_public_text(runner_private), runner_public_key):
        raise IdentityProvisioningError("RUNNER_SIGNING_IDENTITY_MISMATCH")

    return SelectedIdentity(
        root=safe_root,
        bundle_path=bundle,
        version_id=version_id,
        device_id=device_id,
        control_signing_key_id=control_signing_key_id,
        runner_public_key=runner_public_key,
        control_public_key=control_public_key,
        secret_bundle_path=secret_bundle_path,
        control_plane_url=endpoint,
        vercel_environment=vercel_environment,
        vercel_project_id=vercel_project_id,
        vercel_scope_id=vercel_scope_id,
    )


def create_identity_bundle(
    *,
    root: Path,
    repository_root: Path,
    control_plane_url: str,
    runtime_env_path: Path,
    vercel_environment: str,
    vercel_project_id: str,
    vercel_scope_id: str,
    protector: Callable[[bytes], bytes] = protect_with_dpapi,
    version_id: UUID | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Create one immutable identity version and atomically select it."""

    safe_root = validate_external_root(root, repository_root=repository_root)
    if not runtime_env_path.is_absolute():
        raise IdentityProvisioningError("RUNNER_ENV_PATH_NOT_ABSOLUTE")
    try:
        validate_external_root(
            runtime_env_path.parent,
            repository_root=repository_root,
        )
    except IdentityProvisioningError as exc:
        raise IdentityProvisioningError("RUNNER_ENV_PATH_NOT_EXTERNAL") from exc
    endpoint = _validate_control_plane_url(control_plane_url)
    target_environment = _exact_vercel_environment(
        vercel_environment,
        code="VERCEL_ENVIRONMENT_INVALID",
    )
    target_project_id = _exact_vercel_id(
        vercel_project_id,
        prefix="prj_",
        code="VERCEL_PROJECT_INVALID",
    )
    target_scope_id = _exact_vercel_id(
        vercel_scope_id,
        prefix="team_",
        code="VERCEL_SCOPE_INVALID",
    )
    identifier = str(version_id or uuid4())
    checked_at = (now or datetime.now(UTC)).astimezone(UTC)
    versions = safe_root / "versions"
    versions.mkdir(parents=True, exist_ok=True)
    _tighten_windows_acl(safe_root)
    staging = versions / f".{identifier}.tmp"
    final = versions / identifier
    if staging.exists() or final.exists():
        raise IdentityProvisioningError("IDENTITY_VERSION_EXISTS")
    staging.mkdir()
    try:
        runner_private = Ed25519PrivateKey.generate()
        control_private = Ed25519PrivateKey.generate()
        device_id = str(uuid4())
        control_signing_key_id = str(uuid4())
        runner_private_path = final / "runner-private.key"
        control_public_path = final / "control-public.key"
        runtime_env = runtime_env_path.resolve(strict=False)
        secrets_payload = {
            "schema_version": 2,
            "version_id": identifier,
            "control_plane_url": endpoint,
            "vercel_environment": target_environment,
            "vercel_project_id": target_project_id,
            "vercel_scope_id": target_scope_id,
            "control_private_key": _private_text(control_private),
            "operator_token": secrets.token_urlsafe(48),
            "session_secret": secrets.token_urlsafe(48),
            "csrf_secret": secrets.token_urlsafe(48),
        }
        secret_bytes = json.dumps(
            secrets_payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        protected = protector(secret_bytes)
        if not protected or len(protected) > MAX_SECRET_BUNDLE_BYTES:
            raise IdentityProvisioningError("SECRET_BUNDLE_INVALID")

        _write_new(
            staging / "runner-private.key",
            (_private_text(runner_private) + "\n").encode("ascii"),
            private=True,
        )
        _write_new(
            staging / "runner-public.key",
            (_public_text(runner_private) + "\n").encode("ascii"),
            private=False,
        )
        _write_new(
            staging / "control-public.key",
            (_public_text(control_private) + "\n").encode("ascii"),
            private=False,
        )
        _write_new(staging / "control-secrets.dpapi", protected, private=True)
        _write_json_new(
            staging / "runner.json",
            {
                "control_plane_url": endpoint,
                "device_id": device_id,
                "control_signing_key_id": control_signing_key_id,
                "control_plane_audience": CONTROL_AUDIENCE,
                "private_key_path": str(runner_private_path),
                "control_plane_public_key_path": str(control_public_path),
                "runtime_env_path": str(runtime_env),
                "poll_interval_seconds": 10,
                "heartbeat_interval_seconds": 10,
                "offline_after_seconds": 30,
            },
        )
        manifest = {
            "schema_version": 2,
            "version_id": identifier,
            "created_at": checked_at.isoformat().replace("+00:00", "Z"),
            "device_id": device_id,
            "device_public_key": _public_text(runner_private),
            "control_signing_key_id": control_signing_key_id,
            "control_public_key": _public_text(control_private),
            "control_audience": CONTROL_AUDIENCE,
            "runner_audience": RUNNER_AUDIENCE,
            "control_plane_url": endpoint,
            "vercel_environment": target_environment,
            "vercel_project_id": target_project_id,
            "vercel_scope_id": target_scope_id,
            "runner_config_path": str(final / "runner.json"),
            "secret_bundle_path": str(final / "control-secrets.dpapi"),
        }
        _write_json_new(staging / "manifest.json", manifest)
        _tighten_windows_acl(staging)
        _publish_identity_version(staging, final)
        current_tmp = safe_root / f".current.{identifier}.tmp"
        _write_json_new(
            current_tmp,
            {
                "schema_version": 2,
                "version_id": identifier,
                "bundle_path": str(final),
            },
        )
        os.replace(current_tmp, safe_root / "current.json")
        return manifest
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def load_control_secrets(
    bundle_path: Path,
    *,
    unprotector: Callable[[bytes], bytes] = unprotect_with_dpapi,
) -> dict[str, object]:
    """Load protected values in-process; callers must never log the result."""

    try:
        protected = bundle_path.read_bytes()
    except OSError as exc:
        raise IdentityProvisioningError("SECRET_BUNDLE_UNAVAILABLE") from exc
    if not protected or len(protected) > MAX_SECRET_BUNDLE_BYTES:
        raise IdentityProvisioningError("SECRET_BUNDLE_INVALID")
    try:
        values = json.loads(
            unprotector(protected),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise IdentityProvisioningError("SECRET_BUNDLE_INVALID") from exc
    if not isinstance(values, dict) or set(values) != _PROTECTED_IDENTITY_KEYS:
        raise IdentityProvisioningError("SECRET_BUNDLE_INVALID")
    if values.get("schema_version") != 2:
        raise IdentityProvisioningError("SECRET_BUNDLE_INVALID")
    text_names = _PROTECTED_IDENTITY_KEYS - {"schema_version"}
    if any(
        not isinstance(values.get(name), str) or not values[name] or len(values[name]) > 512
        for name in text_names
    ):
        raise IdentityProvisioningError("SECRET_BUNDLE_INVALID")
    return values


def _identity_bundle_attestation(
    values: Mapping[str, str],
    *,
    selection: SelectedIdentity,
) -> str:
    identity_names = _IDENTITY_DERIVED_VERCEL_VARIABLES[:-1]
    identity_values = {name: values[name] for name in identity_names}
    canonical = json.dumps(
        {
            "identity": identity_values,
            "schema_version": 2,
            "target": {
                "environment": selection.vercel_environment,
                "project_id": selection.vercel_project_id,
                "scope_id": selection.vercel_scope_id,
            },
            "version_id": str(selection.version_id),
        },
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    digest = hashlib.sha256(_IDENTITY_ATTESTATION_CONTEXT + canonical).hexdigest()
    return (
        f"v2:{selection.version_id}:{selection.vercel_environment}:"
        f"{selection.vercel_project_id}:{selection.vercel_scope_id}:{digest}"
    )


def _identity_derived_vercel_values(
    selection: SelectedIdentity,
    *,
    unprotector: Callable[[bytes], bytes] = unprotect_with_dpapi,
) -> dict[str, str]:
    values = load_control_secrets(
        selection.secret_bundle_path,
        unprotector=unprotector,
    )
    expected_binding = {
        "schema_version": 2,
        "version_id": str(selection.version_id),
        "control_plane_url": selection.control_plane_url,
        "vercel_environment": selection.vercel_environment,
        "vercel_project_id": selection.vercel_project_id,
        "vercel_scope_id": selection.vercel_scope_id,
    }
    if any(values.get(name) != value for name, value in expected_binding.items()):
        raise IdentityProvisioningError("SECRET_BUNDLE_TARGET_MISMATCH")
    for name in ("operator_token", "session_secret", "csrf_secret"):
        value = values[name]
        assert isinstance(value, str)
        lowered = value.casefold()
        if (
            len(value.encode("utf-8")) < 32
            or len(set(value)) < 12
            or any(marker in lowered for marker in ("changeme", "placeholder", "default-secret"))
        ):
            raise IdentityProvisioningError("SECRET_BUNDLE_INVALID")
    protected_values = {
        values["control_private_key"],
        values["operator_token"],
        values["session_secret"],
        values["csrf_secret"],
    }
    if len(protected_values) != 4:
        raise IdentityProvisioningError("SECRET_BUNDLE_INVALID")
    control_private_text = values["control_private_key"]
    assert isinstance(control_private_text, str)
    try:
        control_private = Ed25519PrivateKey.from_private_bytes(
            _decode_base64url_key(
                control_private_text,
                code="SECRET_BUNDLE_INVALID",
            )
        )
    except ValueError as exc:
        raise IdentityProvisioningError("SECRET_BUNDLE_INVALID") from exc
    if not secrets.compare_digest(
        _public_text(control_private),
        selection.control_public_key,
    ):
        raise IdentityProvisioningError("CONTROL_SIGNING_IDENTITY_MISMATCH")

    mapped = {
        "CONTROL_OPERATOR_TOKEN": str(values["operator_token"]),
        "CONTROL_SESSION_SECRET": str(values["session_secret"]),
        "CONTROL_CSRF_SECRET": str(values["csrf_secret"]),
        "CONTROL_SIGNING_PRIVATE_KEY_B64": control_private_text,
        "CONTROL_SIGNING_KEY_ID": str(selection.control_signing_key_id),
        "CONTROL_RUNNER_PUBLIC_KEY_B64": selection.runner_public_key,
        "CONTROL_RUNNER_DEVICE_ID": str(selection.device_id),
    }
    mapped["CONTROL_IDENTITY_BUNDLE_DIGEST"] = _identity_bundle_attestation(
        mapped,
        selection=selection,
    )
    if tuple(mapped) != _IDENTITY_DERIVED_VERCEL_VARIABLES:
        raise IdentityProvisioningError("VERCEL_IDENTITY_MAPPING_INVALID")
    return mapped


def validate_selected_identity(
    *,
    root: Path,
    repository_root: Path,
    unprotector: Callable[[bytes], bytes] = unprotect_with_dpapi,
) -> SelectedIdentity:
    """Validate public paths plus both private/public signing-key bindings."""

    selection = load_selected_identity(
        root=root,
        repository_root=repository_root,
    )
    _identity_derived_vercel_values(
        selection,
        unprotector=unprotector,
    )
    return selection


def _validate_vercel_cwd(
    path: Path,
    *,
    repository_root: Path,
    project_id: str,
    scope_id: str,
) -> Path:
    if (
        not path.is_absolute()
        or _is_unc(path)
        or not _is_local_fixed_ntfs_path(path)
        or _is_onedrive_path(path)
        or _has_reparse_ancestor(path)
    ):
        raise IdentityProvisioningError("VERCEL_CWD_INVALID")
    try:
        resolved = path.resolve(strict=True)
        expected = (repository_root.resolve(strict=True) / "control_plane").resolve(strict=True)
    except OSError as exc:
        raise IdentityProvisioningError("VERCEL_CWD_INVALID") from exc
    if resolved != expected or not resolved.is_dir():
        raise IdentityProvisioningError("VERCEL_CWD_INVALID")
    link = _read_bounded_json_object(
        resolved / ".vercel" / "project.json",
        required_keys=frozenset({"projectId", "orgId"}),
        maximum_bytes=64 * 1024,
        code="VERCEL_PROJECT_LINK_INVALID",
    )
    if (
        not isinstance(link["projectId"], str)
        or not isinstance(link["orgId"], str)
        or link["projectId"] != project_id
        or link["orgId"] != scope_id
    ):
        raise IdentityProvisioningError("VERCEL_PROJECT_LINK_MISMATCH")
    return resolved


def _sha256_file(path: Path, *, error_code: str = "VERCEL_CLI_INVALID") -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise IdentityProvisioningError(error_code) from exc
    return digest.hexdigest()


def _validate_vercel_cli(path: Path, *, expected_digest: str) -> Path:
    if (
        not path.is_absolute()
        or _is_unc(path)
        or not _is_local_fixed_ntfs_path(path)
        or _is_onedrive_path(path)
        or _has_reparse_ancestor(path)
    ):
        raise IdentityProvisioningError("VERCEL_CLI_INVALID")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise IdentityProvisioningError("VERCEL_CLI_INVALID") from exc
    if not resolved.is_file() or resolved.suffix.casefold() != ".exe":
        raise IdentityProvisioningError("VERCEL_CLI_INVALID")
    if not secrets.compare_digest(
        _sha256_file(resolved),
        _exact_cli_digest(expected_digest),
    ):
        raise IdentityProvisioningError("VERCEL_CLI_DIGEST_MISMATCH")
    return resolved


def _validate_vercel_node(path: Path, *, expected_digest: str) -> Path:
    if (
        not path.is_absolute()
        or _is_unc(path)
        or not _is_local_fixed_ntfs_path(path)
        or _is_onedrive_path(path)
        or _has_reparse_ancestor(path)
    ):
        raise IdentityProvisioningError("VERCEL_NODE_INVALID")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise IdentityProvisioningError("VERCEL_NODE_INVALID") from exc
    if not resolved.is_file() or resolved.suffix.casefold() != ".exe":
        raise IdentityProvisioningError("VERCEL_NODE_INVALID")
    if not secrets.compare_digest(
        _sha256_file(resolved, error_code="VERCEL_NODE_INVALID"),
        _exact_cli_digest(expected_digest),
    ):
        raise IdentityProvisioningError("VERCEL_NODE_DIGEST_MISMATCH")
    return resolved


def _validate_vercel_js_entrypoint(path: Path, *, expected_digest: str) -> Path:
    if (
        not path.is_absolute()
        or _is_unc(path)
        or not _is_local_fixed_ntfs_path(path)
        or _is_onedrive_path(path)
        or _has_reparse_ancestor(path)
    ):
        raise IdentityProvisioningError("VERCEL_JS_ENTRYPOINT_INVALID")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise IdentityProvisioningError("VERCEL_JS_ENTRYPOINT_INVALID") from exc
    if not resolved.is_file() or resolved.suffix.casefold() != ".js":
        raise IdentityProvisioningError("VERCEL_JS_ENTRYPOINT_INVALID")
    if not secrets.compare_digest(
        _sha256_file(resolved, error_code="VERCEL_JS_ENTRYPOINT_INVALID"),
        _exact_cli_digest(expected_digest),
    ):
        raise IdentityProvisioningError("VERCEL_JS_ENTRYPOINT_DIGEST_MISMATCH")
    return resolved


def _validate_vercel_ca_certificate(path: Path, *, expected_digest: str) -> Path:
    if (
        not path.is_absolute()
        or _is_unc(path)
        or not _is_local_fixed_ntfs_path(path)
        or _is_onedrive_path(path)
        or _has_reparse_ancestor(path)
    ):
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_INVALID")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_INVALID") from exc
    if not resolved.is_file() or resolved.suffix.casefold() != ".pem":
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_INVALID")
    try:
        size = resolved.stat().st_size
        payload = resolved.read_bytes()
    except OSError as exc:
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_INVALID") from exc
    if size <= 0 or size > _MAX_VERCEL_CA_BYTES or len(payload) != size:
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_INVALID")
    try:
        certificates = x509.load_pem_x509_certificates(payload)
    except ValueError as exc:
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_INVALID") from exc
    if not certificates:
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_INVALID")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest):
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_DIGEST_INVALID")
    if not secrets.compare_digest(
        _sha256_file(resolved, error_code="VERCEL_CA_CERTIFICATE_INVALID"),
        expected_digest,
    ):
        raise IdentityProvisioningError("VERCEL_CA_CERTIFICATE_DIGEST_MISMATCH")
    return resolved


def _select_vercel_ca_trust(
    certificate: Path | None,
    expected_digest: str | None,
) -> VercelCaTrust | None:
    requested = (certificate is not None, expected_digest is not None)
    if requested == (False, False):
        return None
    if requested != (True, True):
        raise IdentityProvisioningError("VERCEL_CA_TRUST_MODE_INVALID")
    assert certificate is not None
    assert expected_digest is not None
    resolved = _validate_vercel_ca_certificate(
        certificate,
        expected_digest=expected_digest,
    )
    return VercelCaTrust(certificate=resolved, sha256=expected_digest)


def _revalidate_vercel_ca_trust(trust: VercelCaTrust | None) -> None:
    if trust is None:
        return
    _validate_vercel_ca_certificate(
        trust.certificate,
        expected_digest=trust.sha256,
    )


def _select_vercel_cli_invocation(
    *,
    vercel_cli: Path | None,
    vercel_cli_sha256: str | None,
    vercel_node: Path | None,
    vercel_node_sha256: str | None,
    vercel_js_entrypoint: Path | None,
    vercel_js_entrypoint_sha256: str | None,
) -> VercelCliInvocation:
    native_values = (vercel_cli, vercel_cli_sha256)
    node_values = (
        vercel_node,
        vercel_node_sha256,
        vercel_js_entrypoint,
        vercel_js_entrypoint_sha256,
    )
    native_requested = any(value is not None for value in native_values)
    node_requested = any(value is not None for value in node_values)
    if native_requested == node_requested:
        raise IdentityProvisioningError("VERCEL_CLI_MODE_INVALID")
    if native_requested:
        if any(value is None for value in native_values):
            raise IdentityProvisioningError("VERCEL_CLI_MODE_INVALID")
        assert vercel_cli is not None
        assert vercel_cli_sha256 is not None
        executable = _validate_vercel_cli(
            vercel_cli,
            expected_digest=vercel_cli_sha256,
        )
        return VercelCliInvocation(
            mode="native",
            executable=executable,
            prefix_arguments=(),
            native_sha256=_exact_cli_digest(vercel_cli_sha256),
        )
    if any(value is None for value in node_values):
        raise IdentityProvisioningError("VERCEL_CLI_MODE_INVALID")
    assert vercel_node is not None
    assert vercel_node_sha256 is not None
    assert vercel_js_entrypoint is not None
    assert vercel_js_entrypoint_sha256 is not None
    executable = _validate_vercel_node(
        vercel_node,
        expected_digest=vercel_node_sha256,
    )
    entrypoint = _validate_vercel_js_entrypoint(
        vercel_js_entrypoint,
        expected_digest=vercel_js_entrypoint_sha256,
    )
    return VercelCliInvocation(
        mode="node_js",
        executable=executable,
        prefix_arguments=(str(entrypoint),),
        node_sha256=_exact_cli_digest(vercel_node_sha256),
        js_entrypoint=entrypoint,
        js_entrypoint_sha256=_exact_cli_digest(vercel_js_entrypoint_sha256),
    )


def _revalidate_vercel_cli_invocation(
    invocation: VercelCliInvocation,
) -> None:
    if invocation.mode == "native":
        if invocation.native_sha256 is None:
            raise IdentityProvisioningError("VERCEL_CLI_MODE_INVALID")
        _validate_vercel_cli(
            invocation.executable,
            expected_digest=invocation.native_sha256,
        )
        return
    if invocation.mode == "node_js":
        if (
            invocation.node_sha256 is None
            or invocation.js_entrypoint is None
            or invocation.js_entrypoint_sha256 is None
        ):
            raise IdentityProvisioningError("VERCEL_CLI_MODE_INVALID")
        _validate_vercel_node(
            invocation.executable,
            expected_digest=invocation.node_sha256,
        )
        _validate_vercel_js_entrypoint(
            invocation.js_entrypoint,
            expected_digest=invocation.js_entrypoint_sha256,
        )
        return
    raise IdentityProvisioningError("VERCEL_CLI_MODE_INVALID")


def _sanitized_vercel_environment(
    source: Mapping[str, str] | None = None,
    *,
    ca_trust: VercelCaTrust | None = None,
) -> dict[str, str]:
    values = os.environ if source is None else source
    allowed = {
        "appdata": "APPDATA",
        "localappdata": "LOCALAPPDATA",
        "systemroot": "SystemRoot",
        "temp": "TEMP",
        "tmp": "TMP",
        "userprofile": "USERPROFILE",
        "windir": "WINDIR",
    }
    sanitized: dict[str, str] = {}
    for name, value in values.items():
        canonical = allowed.get(name.casefold())
        if canonical and value:
            sanitized[canonical] = value
    sanitized["CI"] = "1"
    sanitized["NO_COLOR"] = "1"
    if ca_trust is not None:
        sanitized["NODE_EXTRA_CA_CERTS"] = str(ca_trust.certificate)
    return sanitized


def _read_vercel_cli_version(
    executable: Path,
    working_directory: Path,
    environment: Mapping[str, str],
    *,
    prefix_arguments: Sequence[str] = (),
) -> str:
    try:
        completed = subprocess.run(
            [str(executable), *prefix_arguments, "--version"],
            cwd=working_directory,
            env=dict(environment),
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        raise IdentityProvisioningError("VERCEL_CLI_VERSION_TIMEOUT") from None
    except OSError:
        raise IdentityProvisioningError("VERCEL_CLI_UNAVAILABLE") from None
    if completed.returncode != 0:
        raise IdentityProvisioningError("VERCEL_CLI_VERSION_UNAVAILABLE")
    output = f"{completed.stdout}\n{completed.stderr}"
    if len(output) > 4_096:
        raise IdentityProvisioningError("VERCEL_CLI_VERSION_INVALID")
    matches = set(re.findall(r"(?<![0-9.])([0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)", output))
    if len(matches) != 1:
        raise IdentityProvisioningError("VERCEL_CLI_VERSION_INVALID")
    return matches.pop()


def _run_vercel_command(
    executable: Path,
    arguments: Sequence[str],
    stdin_value: str,
    working_directory: Path,
    environment: Mapping[str, str],
) -> int:
    """Run one non-interactive CLI call with all output discarded."""

    try:
        completed = subprocess.run(
            [str(executable), *arguments],
            cwd=working_directory,
            env=dict(environment),
            input=stdin_value,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        raise IdentityProvisioningError("VERCEL_CLI_TIMEOUT") from None
    except OSError:
        raise IdentityProvisioningError("VERCEL_CLI_UNAVAILABLE") from None
    return int(completed.returncode)


def _read_vercel_environment_metadata(
    executable: Path,
    arguments: Sequence[str],
    working_directory: Path,
    environment: Mapping[str, str],
) -> str:
    """Read bounded non-decrypted environment metadata from the pinned CLI."""

    try:
        process = subprocess.Popen(
            [str(executable), *arguments],
            cwd=working_directory,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
    except OSError:
        raise IdentityProvisioningError("VERCEL_CLI_UNAVAILABLE") from None
    if process.stdout is None:
        process.kill()
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_UNAVAILABLE")

    chunks: list[bytes] = []
    overflow = threading.Event()
    read_failed = threading.Event()

    def kill_process() -> None:
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass

    def drain_stdout() -> None:
        total = 0
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    return
                total += len(chunk)
                if total > _MAX_VERCEL_METADATA_BYTES:
                    overflow.set()
                    kill_process()
                    return
                chunks.append(chunk)
        except (OSError, ValueError):
            read_failed.set()
            kill_process()

    reader = threading.Thread(
        target=drain_stdout,
        name="vercel-metadata-reader",
        daemon=True,
    )
    reader.start()
    timed_out = False
    try:
        return_code = process.wait(timeout=120)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_process()
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return_code = -1
    finally:
        reader.join(timeout=5)
        process.stdout.close()

    if timed_out or reader.is_alive():
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_TIMEOUT")
    if overflow.is_set():
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_TOO_LARGE")
    if read_failed.is_set():
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_UNAVAILABLE")
    if return_code != 0:
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_UNAVAILABLE")
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError:
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID") from None


def _vercel_environment_inventory(
    raw_metadata: str,
    *,
    environment: str,
) -> VercelEnvironmentInventory:
    """Parse one complete, non-decrypted exact-target environment inventory."""

    if not isinstance(raw_metadata, str):
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
    if environment not in {"preview", "production"}:
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
    if len(raw_metadata.encode("utf-8")) > _MAX_VERCEL_METADATA_BYTES:
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_TOO_LARGE")
    try:
        payload = json.loads(
            raw_metadata,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=lambda _constant: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, ValueError, TypeError):
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("envs"), list):
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")

    has_hidden_count = "hiddenProductionEnvCount" in payload
    has_pagination = "pagination" in payload
    if has_hidden_count == has_pagination:
        raise IdentityProvisioningError("VERCEL_ENV_METADATA_INCOMPLETE")
    if has_hidden_count:
        hidden_production_count = payload["hiddenProductionEnvCount"]
        if type(hidden_production_count) is not int or hidden_production_count < 0:
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
        if hidden_production_count:
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INCOMPLETE")
    else:
        pagination = payload["pagination"]
        if not isinstance(pagination, dict) or not {
            "count",
            "next",
            "prev",
        }.issubset(pagination):
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
        count = pagination["count"]
        if type(count) is not int or count < 0:
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
        if (
            count != len(payload["envs"])
            or pagination["next"] is not None
            or pagination["prev"] is not None
        ):
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INCOMPLETE")

    records: dict[tuple[str, str], str] = {}
    all_record_ids: set[str] = set()
    for raw_record in payload["envs"]:
        if not isinstance(raw_record, dict):
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
        identifier = raw_record.get("id")
        if not isinstance(identifier, str) or _VERCEL_ENV_ID_PATTERN.fullmatch(identifier) is None:
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
        if identifier in all_record_ids:
            raise IdentityProvisioningError("VERCEL_ENV_RECORD_ID_ALIAS")
        all_record_ids.add(identifier)

        name = raw_record.get("key")
        if name not in _IDENTITY_DERIVED_VERCEL_VARIABLES:
            continue
        secret_metadata_values = tuple(
            raw_record.get(field) for field in ("value", "legacyValue", "vsmValue")
        )
        if any(
            value is not None and not isinstance(value, str) for value in secret_metadata_values
        ):
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
        if raw_record.get("decrypted") is not False or any(
            value not in {None, ""} for value in secret_metadata_values
        ):
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_UNSAFE")
        if raw_record.get("type") != "sensitive":
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")

        raw_target = raw_record.get("target")
        if isinstance(raw_target, str):
            targets = (raw_target,)
        elif isinstance(raw_target, list) and all(isinstance(value, str) for value in raw_target):
            targets = tuple(raw_target)
        else:
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
        raw_git_branch = raw_record.get("gitBranch")
        if raw_git_branch is not None and not isinstance(raw_git_branch, str):
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_INVALID")
        if (
            len(targets) != 1
            or targets[0] not in {"preview", "production"}
            or raw_git_branch not in {None, ""}
            or raw_record.get("customEnvironmentIds") not in (None, [], ())
        ):
            raise IdentityProvisioningError(f"VERCEL_ENV_TARGET_AMBIGUOUS_{name}")
        target = targets[0]
        record_key = (name, target)
        if record_key in records:
            raise IdentityProvisioningError(f"VERCEL_ENV_TARGET_AMBIGUOUS_{name}")
        records[record_key] = identifier

    targeted_records = tuple(
        (name, records[(name, environment)])
        for name in _IDENTITY_DERIVED_VERCEL_VARIABLES
        if (name, environment) in records
    )
    other_target_records = tuple(
        (name, target, records[(name, target)])
        for name in _IDENTITY_DERIVED_VERCEL_VARIABLES
        for target in ("preview", "production")
        if target != environment and (name, target) in records
    )
    return VercelEnvironmentInventory(
        targeted_records=targeted_records,
        other_target_records=other_target_records,
    )


def _targeted_vercel_environment_records(
    raw_metadata: str,
    *,
    environment: str,
) -> dict[str, str]:
    """Select exact built-in-environment records without reading secret values."""

    return dict(
        _vercel_environment_inventory(
            raw_metadata,
            environment=environment,
        ).targeted_records
    )


def configure_vercel_identity(
    *,
    root: Path,
    repository_root: Path,
    vercel_cli: Path | None = None,
    vercel_cli_sha256: str | None = None,
    vercel_node: Path | None = None,
    vercel_node_sha256: str | None = None,
    vercel_js_entrypoint: Path | None = None,
    vercel_js_entrypoint_sha256: str | None = None,
    vercel_ca_certificate: Path | None = None,
    vercel_ca_certificate_sha256: str | None = None,
    vercel_cli_version: str,
    vercel_cwd: Path,
    environment: str,
    project: str,
    scope: str,
    dry_run: bool = False,
    runner: VercelCommandRunner = _run_vercel_command,
    metadata_reader: VercelMetadataReader = _read_vercel_environment_metadata,
    version_reader: VercelVersionReader | None = None,
    unprotector: Callable[[bytes], bytes] = unprotect_with_dpapi,
    platform_name: str | None = None,
) -> VercelConfigurationResult:
    """Configure only identity-derived variables without exposing their values."""

    if (platform_name or os.name) != "nt":
        raise IdentityProvisioningError("WINDOWS_REQUIRED")
    selected_environment = _exact_vercel_environment(
        environment,
        code="VERCEL_ENVIRONMENT_INVALID",
    )
    selected_project = _exact_vercel_id(
        project,
        prefix="prj_",
        code="VERCEL_PROJECT_INVALID",
    )
    selected_scope = _exact_vercel_id(
        scope,
        prefix="team_",
        code="VERCEL_SCOPE_INVALID",
    )
    expected_cli_version = _exact_cli_version(vercel_cli_version)
    invocation = _select_vercel_cli_invocation(
        vercel_cli=vercel_cli,
        vercel_cli_sha256=vercel_cli_sha256,
        vercel_node=vercel_node,
        vercel_node_sha256=vercel_node_sha256,
        vercel_js_entrypoint=vercel_js_entrypoint,
        vercel_js_entrypoint_sha256=vercel_js_entrypoint_sha256,
    )
    ca_trust = _select_vercel_ca_trust(
        vercel_ca_certificate,
        vercel_ca_certificate_sha256,
    )
    working_directory = _validate_vercel_cwd(
        vercel_cwd,
        repository_root=repository_root,
        project_id=selected_project,
        scope_id=selected_scope,
    )
    selection = load_selected_identity(
        root=root,
        repository_root=repository_root,
    )
    if (
        selection.vercel_environment != selected_environment
        or selection.vercel_project_id != selected_project
        or selection.vercel_scope_id != selected_scope
    ):
        raise IdentityProvisioningError("IDENTITY_VERCEL_TARGET_MISMATCH")
    variable_names = _IDENTITY_DERIVED_VERCEL_VARIABLES
    if dry_run:
        return VercelConfigurationResult(
            dry_run=True,
            cli_mode=invocation.mode,
            environment=selected_environment,
            project=selected_project,
            scope=selected_scope,
            variable_names=variable_names,
            configured_count=0,
            expected_cli_version=expected_cli_version,
        )

    _revalidate_vercel_ca_trust(ca_trust)
    sanitized_environment = _sanitized_vercel_environment(ca_trust=ca_trust)
    try:
        if version_reader is None:
            observed_cli_version = _read_vercel_cli_version(
                invocation.executable,
                working_directory,
                sanitized_environment,
                prefix_arguments=invocation.prefix_arguments,
            )
        else:
            observed_cli_version = version_reader(
                invocation.executable,
                working_directory,
                sanitized_environment,
            )
    except IdentityProvisioningError:
        raise
    except Exception:
        raise IdentityProvisioningError("VERCEL_CLI_VERSION_UNAVAILABLE") from None
    if observed_cli_version != expected_cli_version:
        raise IdentityProvisioningError("VERCEL_CLI_VERSION_MISMATCH")

    metadata_arguments = (
        *invocation.prefix_arguments,
        "api",
        f"/v10/projects/{selected_project}/env?decrypt=false",
        "--raw",
        "--cwd",
        str(working_directory),
        "--scope",
        selected_scope,
        "--no-color",
    )

    def read_inventory() -> VercelEnvironmentInventory:
        _revalidate_vercel_cli_invocation(invocation)
        _revalidate_vercel_ca_trust(ca_trust)
        try:
            raw_metadata = metadata_reader(
                invocation.executable,
                metadata_arguments,
                working_directory,
                sanitized_environment,
            )
        except IdentityProvisioningError:
            raise
        except Exception:
            raise IdentityProvisioningError("VERCEL_ENV_METADATA_UNAVAILABLE") from None
        return _vercel_environment_inventory(
            raw_metadata,
            environment=selected_environment,
        )

    initial_inventory = read_inventory()
    existing_records = dict(initial_inventory.targeted_records)

    values = _identity_derived_vercel_values(
        selection,
        unprotector=unprotector,
    )
    configured = 0
    for name in variable_names:
        _revalidate_vercel_cli_invocation(invocation)
        _revalidate_vercel_ca_trust(ca_trust)
        record_id = existing_records.get(name)
        if record_id is None:
            endpoint = f"/v10/projects/{selected_project}/env"
            method = "POST"
        else:
            endpoint = f"/v9/projects/{selected_project}/env/{record_id}"
            method = "PATCH"
        request = {
            "value": values[name],
            "type": "sensitive",
            "target": [selected_environment],
        }
        if method == "POST":
            request["key"] = name
        request_body = json.dumps(request, ensure_ascii=True, separators=(",", ":"))
        arguments = (
            *invocation.prefix_arguments,
            "api",
            endpoint,
            "--method",
            method,
            "--input",
            "-",
            "--silent",
            "--cwd",
            str(working_directory),
            "--scope",
            selected_scope,
            "--no-color",
        )
        try:
            return_code = int(
                runner(
                    invocation.executable,
                    arguments,
                    request_body,
                    working_directory,
                    sanitized_environment,
                )
            )
        except IdentityProvisioningError:
            raise
        except Exception:
            raise IdentityProvisioningError(f"VERCEL_ENV_CONFIGURATION_FAILED_{name}") from None
        if return_code != 0:
            raise IdentityProvisioningError(f"VERCEL_ENV_CONFIGURATION_FAILED_{name}")
        configured += 1

    final_inventory = read_inventory()
    final_records = dict(final_inventory.targeted_records)
    if set(final_records) != set(variable_names):
        raise IdentityProvisioningError("VERCEL_ENV_ATTESTATION_FAILED")
    if any(
        final_records.get(name) != identifier
        for name, identifier in initial_inventory.targeted_records
    ):
        raise IdentityProvisioningError("VERCEL_ENV_ATTESTATION_FAILED")
    if final_inventory.other_target_records != initial_inventory.other_target_records:
        raise IdentityProvisioningError("VERCEL_ENV_ATTESTATION_FAILED")
    return VercelConfigurationResult(
        dry_run=False,
        cli_mode=invocation.mode,
        environment=selected_environment,
        project=selected_project,
        scope=selected_scope,
        variable_names=variable_names,
        configured_count=configured,
        expected_cli_version=expected_cli_version,
    )


class WindowsClipboard:
    """Small CF_UNICODETEXT wrapper; no shell or child process is involved."""

    _CF_UNICODETEXT = 13
    _GMEM_MOVEABLE = 0x0002
    _HWND_MESSAGE = -3

    def __init__(self) -> None:
        if os.name != "nt":
            raise IdentityProvisioningError("WINDOWS_REQUIRED")
        self._user32 = _windows_dll("user32")
        self._kernel32 = _windows_dll("kernel32")
        self._user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            ctypes.c_void_p,
        ]
        self._user32.CreateWindowExW.restype = wintypes.HWND
        self._user32.DestroyWindow.argtypes = [wintypes.HWND]
        self._user32.DestroyWindow.restype = wintypes.BOOL
        self._user32.IsWindow.argtypes = [wintypes.HWND]
        self._user32.IsWindow.restype = wintypes.BOOL
        self._user32.OpenClipboard.argtypes = [wintypes.HWND]
        self._user32.OpenClipboard.restype = wintypes.BOOL
        self._user32.CloseClipboard.argtypes = []
        self._user32.CloseClipboard.restype = wintypes.BOOL
        self._user32.EmptyClipboard.argtypes = []
        self._user32.EmptyClipboard.restype = wintypes.BOOL
        self._user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        self._user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        self._user32.GetClipboardData.argtypes = [wintypes.UINT]
        self._user32.GetClipboardData.restype = ctypes.c_void_p
        self._user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
        self._user32.SetClipboardData.restype = ctypes.c_void_p
        self._user32.GetClipboardSequenceNumber.argtypes = []
        self._user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = ctypes.c_void_p
        self._kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.GlobalFree.restype = ctypes.c_void_p
        self._kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        self._kernel32.GlobalLock.restype = ctypes.c_void_p
        self._kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        self._kernel32.GlobalSize.restype = ctypes.c_size_t
        self._kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        self._kernel32.GlobalUnlock.restype = wintypes.BOOL
        self._kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        self._kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        instance = self._kernel32.GetModuleHandleW(None)
        self._owner_window = self._user32.CreateWindowExW(
            0,
            "STATIC",
            "JobApplyAgentClipboardOwner",
            0,
            0,
            0,
            0,
            0,
            wintypes.HWND(self._HWND_MESSAGE),
            None,
            instance,
            None,
        )
        if not self._owner_window or not self._user32.IsWindow(self._owner_window):
            self._owner_window = None
            raise IdentityProvisioningError("CLIPBOARD_OWNER_UNAVAILABLE")

    def _destroy_owner_window(self) -> None:
        owner = self._owner_window
        self._owner_window = None
        if owner and self._user32.IsWindow(owner):
            self._user32.DestroyWindow(owner)

    def _open(self) -> None:
        owner = self._owner_window
        if not owner or not self._user32.IsWindow(owner):
            raise IdentityProvisioningError("CLIPBOARD_OWNER_UNAVAILABLE")
        if not self._user32.OpenClipboard(owner):
            raise IdentityProvisioningError("CLIPBOARD_UNAVAILABLE")

    def set_text(self, value: str) -> ClipboardLease:
        encoded = (value + "\0").encode("utf-16-le")
        handle = self._kernel32.GlobalAlloc(self._GMEM_MOVEABLE, len(encoded))
        if not handle:
            self._destroy_owner_window()
            raise IdentityProvisioningError("CLIPBOARD_WRITE_FAILED")
        transferred = False
        ready = False
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            self._destroy_owner_window()
            raise IdentityProvisioningError("CLIPBOARD_WRITE_FAILED")
        try:
            ctypes.memmove(pointer, encoded, len(encoded))
        finally:
            self._kernel32.GlobalUnlock(handle)
        try:
            self._open()
        except BaseException:
            self._kernel32.GlobalFree(handle)
            self._destroy_owner_window()
            raise
        sequence_number = 0
        try:
            if not self._user32.EmptyClipboard():
                raise IdentityProvisioningError("CLIPBOARD_WRITE_FAILED")
            if not self._user32.SetClipboardData(self._CF_UNICODETEXT, handle):
                raise IdentityProvisioningError("CLIPBOARD_WRITE_FAILED")
            transferred = True
            sequence_number = int(self._user32.GetClipboardSequenceNumber())
            if sequence_number <= 0:
                self._user32.EmptyClipboard()
                raise IdentityProvisioningError("CLIPBOARD_SEQUENCE_UNAVAILABLE")
            ready = True
        finally:
            self._user32.CloseClipboard()
            if not transferred:
                self._kernel32.GlobalFree(handle)
            if not ready:
                self._destroy_owner_window()
        return ClipboardLease(sequence_number=sequence_number)

    def clear_if_unchanged(self, expected: str, lease: ClipboardLease) -> bool:
        self._open()
        try:
            sequence_number = int(self._user32.GetClipboardSequenceNumber())
            if sequence_number <= 0:
                raise IdentityProvisioningError("CLIPBOARD_SEQUENCE_UNAVAILABLE")
            if sequence_number != lease.sequence_number:
                return False
            if not self._user32.IsClipboardFormatAvailable(self._CF_UNICODETEXT):
                return False
            handle = self._user32.GetClipboardData(self._CF_UNICODETEXT)
            if not handle:
                return False
            size = int(self._kernel32.GlobalSize(handle))
            if not 2 <= size <= 4096 or size % 2:
                return False
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                raise IdentityProvisioningError("CLIPBOARD_READ_FAILED")
            try:
                raw = ctypes.string_at(pointer, size)
            finally:
                self._kernel32.GlobalUnlock(handle)
            terminator = next(
                (
                    offset
                    for offset in range(0, len(raw) - 1, 2)
                    if raw[offset : offset + 2] == b"\0\0"
                ),
                None,
            )
            if terminator is None:
                return False
            try:
                current = raw[:terminator].decode("utf-16-le")
            except UnicodeDecodeError:
                return False
            if not secrets.compare_digest(current, expected):
                return False
            final_sequence_number = int(self._user32.GetClipboardSequenceNumber())
            if final_sequence_number <= 0:
                raise IdentityProvisioningError("CLIPBOARD_SEQUENCE_UNAVAILABLE")
            if final_sequence_number != lease.sequence_number:
                return False
            if not self._user32.EmptyClipboard():
                raise IdentityProvisioningError("CLIPBOARD_CLEAR_FAILED")
            return True
        finally:
            self._user32.CloseClipboard()
            self._destroy_owner_window()


def _write_clipboard_warning(message: str) -> None:
    print(message, file=sys.stderr)  # noqa: T201


def _emit_clipboard_warning(
    message: str,
    *,
    writer: Callable[[str], None],
) -> BaseException | None:
    """Emit a warning, falling back to stderr without hiding writer failures."""

    try:
        writer(message)
        return None
    except BaseException as exc:
        if writer is not _write_clipboard_warning:
            try:
                _write_clipboard_warning(message)
            except BaseException:
                pass
        return exc


def copy_operator_token(
    *,
    root: Path,
    repository_root: Path,
    ttl_seconds: int,
    clipboard: ClipboardController | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    retry_sleeper: Callable[[float], None] = time.sleep,
    warning_writer: Callable[[str], None] = _write_clipboard_warning,
    unprotector: Callable[[bytes], bytes] = unprotect_with_dpapi,
    platform_name: str | None = None,
) -> bool:
    """Copy the operator token briefly and clear it only if it is unchanged."""

    if (platform_name or os.name) != "nt":
        raise IdentityProvisioningError("WINDOWS_REQUIRED")
    if not _MIN_CLIPBOARD_TTL_SECONDS <= ttl_seconds <= _MAX_CLIPBOARD_TTL_SECONDS:
        raise IdentityProvisioningError("CLIPBOARD_TTL_INVALID")
    selection = load_selected_identity(
        root=root,
        repository_root=repository_root,
    )
    values = _identity_derived_vercel_values(
        selection,
        unprotector=unprotector,
    )
    token = values["CONTROL_OPERATOR_TOKEN"]
    controller = clipboard or WindowsClipboard()
    lease = controller.set_text(token)
    cleared = False
    cleanup_failed = False
    primary_interruption: BaseException | None = None
    retry_interruption: BaseException | None = None
    primary_interruption = _emit_clipboard_warning(
        "WARNING: the operator token is on the Windows clipboard for "
        f"at most {ttl_seconds} seconds; keep this command running.",
        writer=warning_writer,
    )
    if primary_interruption is None:
        try:
            sleeper(float(ttl_seconds))
        except BaseException as exc:
            primary_interruption = exc
    for attempt in range(_CLIPBOARD_CLEAR_ATTEMPTS):
        try:
            cleared = controller.clear_if_unchanged(token, lease)
            break
        except IdentityProvisioningError as exc:
            if str(exc) != "CLIPBOARD_UNAVAILABLE":
                cleanup_failed = True
                break
            if attempt == _CLIPBOARD_CLEAR_ATTEMPTS - 1:
                cleanup_failed = True
                break
            try:
                retry_sleeper(_CLIPBOARD_CLEAR_RETRY_SECONDS)
            except BaseException as exc:
                # A failing delay must not suppress the next safe cleanup
                # attempt while the unchanged lease may still be cleared.
                retry_interruption = retry_interruption or exc
                continue
        except BaseException:
            cleanup_failed = True
            break
    if cleanup_failed:
        _emit_clipboard_warning(
            "WARNING: the clipboard could not be safely verified and cleared; "
            "clear the operator token manually now.",
            writer=warning_writer,
        )
        raise IdentityProvisioningError("CLIPBOARD_CLEANUP_FAILED") from None
    if cleared:
        final_warning = "WARNING: the unchanged operator token was cleared from the clipboard."
    else:
        final_warning = "WARNING: clipboard content changed and was left untouched."
    final_warning_error = _emit_clipboard_warning(
        final_warning,
        writer=warning_writer,
    )
    if primary_interruption is not None:
        raise primary_interruption
    if retry_interruption is not None:
        raise retry_interruption
    if final_warning_error is not None:
        raise final_warning_error
    return cleared


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="control_plane_identity.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--root", type=Path, default=None)
    create.add_argument("--repository-root", type=Path, required=True)
    create.add_argument("--control-plane-url", required=True)
    create.add_argument("--runtime-env-path", type=Path, required=True)
    create.add_argument(
        "--vercel-environment",
        choices=("production", "preview"),
        required=True,
    )
    create.add_argument("--vercel-project-id", required=True)
    create.add_argument("--vercel-scope-id", required=True)
    configure = subparsers.add_parser("configure-vercel")
    configure.add_argument("--root", type=Path, default=None)
    configure.add_argument("--repository-root", type=Path, required=True)
    configure.add_argument("--vercel-cli", type=Path)
    configure.add_argument("--vercel-cli-sha256")
    configure.add_argument("--vercel-node", type=Path)
    configure.add_argument("--vercel-node-sha256")
    configure.add_argument("--vercel-js-entrypoint", type=Path)
    configure.add_argument("--vercel-js-entrypoint-sha256")
    configure.add_argument("--vercel-ca-certificate", type=Path)
    configure.add_argument("--vercel-ca-certificate-sha256")
    configure.add_argument("--vercel-cli-version", required=True)
    configure.add_argument("--vercel-cwd", type=Path, required=True)
    configure.add_argument(
        "--environment",
        choices=("production", "preview"),
        required=True,
    )
    configure.add_argument("--project", required=True)
    configure.add_argument("--scope", required=True)
    configure.add_argument("--dry-run", action="store_true")
    copy_token = subparsers.add_parser("copy-operator-token")
    copy_token.add_argument("--root", type=Path, default=None)
    copy_token.add_argument("--repository-root", type=Path, required=True)
    copy_token.add_argument(
        "--ttl-seconds",
        type=int,
        default=60,
    )
    validate = subparsers.add_parser("validate-selection")
    validate.add_argument("--root", type=Path, default=None)
    validate.add_argument("--repository-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root or _default_root()
    if args.command == "create":
        manifest = create_identity_bundle(
            root=root,
            repository_root=args.repository_root,
            control_plane_url=args.control_plane_url,
            runtime_env_path=args.runtime_env_path,
            vercel_environment=args.vercel_environment,
            vercel_project_id=args.vercel_project_id,
            vercel_scope_id=args.vercel_scope_id,
        )
        public_result = {
            "version_id": manifest["version_id"],
            "device_id": manifest["device_id"],
            "control_signing_key_id": manifest["control_signing_key_id"],
            "runner_config_path": manifest["runner_config_path"],
        }
        print(json.dumps(public_result, sort_keys=True))  # noqa: T201
        return 0
    if args.command == "configure-vercel":
        result = configure_vercel_identity(
            root=root,
            repository_root=args.repository_root,
            vercel_cli=args.vercel_cli,
            vercel_cli_sha256=args.vercel_cli_sha256,
            vercel_node=args.vercel_node,
            vercel_node_sha256=args.vercel_node_sha256,
            vercel_js_entrypoint=args.vercel_js_entrypoint,
            vercel_js_entrypoint_sha256=args.vercel_js_entrypoint_sha256,
            vercel_ca_certificate=args.vercel_ca_certificate,
            vercel_ca_certificate_sha256=args.vercel_ca_certificate_sha256,
            vercel_cli_version=args.vercel_cli_version,
            vercel_cwd=args.vercel_cwd,
            environment=args.environment,
            project=args.project,
            scope=args.scope,
            dry_run=args.dry_run,
        )
        print(  # noqa: T201
            json.dumps(
                {
                    "cli_mode": result.cli_mode,
                    "configured_count": result.configured_count,
                    "expected_cli_version": result.expected_cli_version,
                    "dry_run": result.dry_run,
                    "environment": result.environment,
                    "project": result.project,
                    "scope": result.scope,
                    "variable_names": result.variable_names,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "copy-operator-token":
        copy_operator_token(
            root=root,
            repository_root=args.repository_root,
            ttl_seconds=args.ttl_seconds,
        )
        return 0
    if args.command == "validate-selection":
        selection = validate_selected_identity(
            root=root,
            repository_root=args.repository_root,
        )
        print(  # noqa: T201
            json.dumps(
                {
                    "control_plane_url": selection.control_plane_url,
                    "device_id": str(selection.device_id),
                    "vercel_environment": selection.vercel_environment,
                    "vercel_project_id": selection.vercel_project_id,
                    "vercel_scope_id": selection.vercel_scope_id,
                    "version_id": str(selection.version_id),
                },
                sort_keys=True,
            )
        )
        return 0
    raise IdentityProvisioningError("COMMAND_INVALID")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IdentityProvisioningError as exc:
        print(str(exc), file=sys.stderr)  # noqa: T201
        raise SystemExit(2) from exc
