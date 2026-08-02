from __future__ import annotations

import ctypes
import errno
import hashlib
import io
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from control_plane.job_control_plane.config import build_identity_bundle_digest
from scripts import control_plane_identity as identity_module
from scripts.control_plane_identity import (
    ClipboardLease,
    IdentityProvisioningError,
    WindowsClipboard,
    _is_local_fixed_ntfs_path,
    _read_vercel_cli_version,
    _run_vercel_command,
    _validate_vercel_cwd,
    _windows_path_is_reparse_point,
    configure_vercel_identity,
    copy_operator_token,
    create_identity_bundle,
    load_control_secrets,
    load_selected_identity,
    validate_external_root,
    validate_selected_identity,
)

PROJECT_ID = "prj_12345678abcdef"
SCOPE_ID = "team_12345678abcdef"
CLI_VERSION = "58.1.0"


class _FakeCFunction:
    def __init__(self, implementation: object) -> None:
        self.implementation = implementation
        self.argtypes: object = None
        self.restype: object = None

    def __call__(self, *arguments: object) -> object:
        return self.implementation(*arguments)  # type: ignore[operator]


def _protector(value: bytes) -> bytes:
    return b"test-protected:" + value[::-1]


def _unprotector(value: bytes) -> bytes:
    prefix = b"test-protected:"
    assert value.startswith(prefix)
    return value.removeprefix(prefix)[::-1]


def _test_ca_certificate(tmp_path: Path) -> tuple[Path, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test enterprise CA")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )
    path = (tmp_path / "enterprise-ca.pem").resolve()
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _empty_vercel_metadata(
    _executable: Path,
    _arguments: object,
    _cwd: Path,
    _environment: object,
) -> str:
    return json.dumps({"envs": [], "hiddenProductionEnvCount": 0})


def _test_identity(
    tmp_path: Path,
    *,
    vercel_environment: str = "production",
) -> tuple[Path, Path, Path, Path, Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    vercel_cwd = repository / "control_plane"
    link_root = vercel_cwd / ".vercel"
    link_root.mkdir(parents=True)
    (link_root / "project.json").write_text(
        json.dumps({"orgId": SCOPE_ID, "projectId": PROJECT_ID}),
        encoding="utf-8",
    )
    root = tmp_path / "local-app-data" / "JobApplyAgent" / "control-plane"
    runtime = tmp_path / "local-app-data" / "JobApplyAgent" / "runtime"
    runtime.mkdir(parents=True)
    runtime_env = runtime / "runtime.env"
    with patch("scripts.control_plane_identity._tighten_windows_acl"):
        create_identity_bundle(
            root=root.resolve(),
            repository_root=repository.resolve(),
            control_plane_url="https://control.example",
            runtime_env_path=runtime_env.resolve(),
            vercel_environment=vercel_environment,
            vercel_project_id=PROJECT_ID,
            vercel_scope_id=SCOPE_ID,
            protector=_protector,
        )
    cli = tmp_path / "tools" / "vercel.exe"
    cli.parent.mkdir()
    cli.write_bytes(b"test-native-vercel-cli")
    cli_digest = hashlib.sha256(cli.read_bytes()).hexdigest()
    return (
        repository.resolve(),
        root.resolve(),
        runtime_env.resolve(),
        cli.resolve(),
        vercel_cwd.resolve(),
        cli_digest,
    )


def _test_node_cli(tmp_path: Path) -> tuple[Path, str, Path, str]:
    node = tmp_path / "tools" / "node.exe"
    entrypoint = tmp_path / "tools" / "node_modules" / "vercel" / "dist" / "vc.js"
    entrypoint.parent.mkdir(parents=True)
    node.write_bytes(b"test-pinned-node")
    entrypoint.write_bytes(b"test-pinned-vercel-entrypoint")
    return (
        node.resolve(),
        hashlib.sha256(node.read_bytes()).hexdigest(),
        entrypoint.resolve(),
        hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows text mode translates binary newlines")
def test_binary_identity_writer_preserves_dpapi_ciphertext_bytes(tmp_path: Path) -> None:
    payload = b"\x01\x00ciphertext\nwith\r\nbinary\x00bytes\xff"
    destination = tmp_path / "control-secrets.dpapi"

    identity_module._write_new(destination, payload, private=True)

    assert destination.read_bytes() == payload


def test_binary_identity_writer_completes_short_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"binary-identity-payload-with-several-write-chunks"
    destination = tmp_path / "control-secrets.dpapi"
    actual_write = os.write
    write_sizes: list[int] = []

    def short_write(descriptor: int, value: bytes | memoryview) -> int:
        chunk = value[:5]
        written = actual_write(descriptor, chunk)
        write_sizes.append(written)
        return written

    monkeypatch.setattr(identity_module.os, "write", short_write)

    identity_module._write_new(destination, payload, private=True)

    assert destination.read_bytes() == payload
    assert len(write_sizes) > 1


def test_binary_identity_writer_removes_partial_file_without_write_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = tmp_path / "control-secrets.dpapi"
    monkeypatch.setattr(identity_module.os, "write", lambda _descriptor, _value: 0)

    with pytest.raises(OSError, match="identity write made no progress"):
        identity_module._write_new(destination, b"protected-identity", private=True)

    assert not destination.exists()


class _FakeClipboard:
    def __init__(self) -> None:
        self.value: str | None = None
        self.clear_calls = 0
        self.sequence = 0

    def set_text(self, value: str) -> ClipboardLease:
        self.value = value
        self.sequence += 1
        return ClipboardLease(sequence_number=self.sequence)

    def clear_if_unchanged(self, expected: str, lease: ClipboardLease) -> bool:
        self.clear_calls += 1
        if self.value != expected or lease.sequence_number != self.sequence:
            return False
        self.value = None
        return True


def test_identity_bundle_is_versioned_external_and_public_manifest_is_redacted(
    tmp_path: Path,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    root = tmp_path / "local-app-data" / "JobApplyAgent" / "control-plane"
    runtime = tmp_path / "local-app-data" / "JobApplyAgent" / "runtime"
    runtime.mkdir(parents=True)
    runtime_env = runtime / "runtime.env"
    version = UUID("00000000-0000-0000-0000-000000000123")

    manifest = create_identity_bundle(
        root=root.resolve(),
        repository_root=repository.resolve(),
        control_plane_url="https://control.example",
        runtime_env_path=runtime_env.resolve(),
        vercel_environment="production",
        vercel_project_id=PROJECT_ID,
        vercel_scope_id=SCOPE_ID,
        protector=_protector,
        version_id=version,
    )

    bundle = root / "versions" / str(version)
    assert bundle.is_dir()
    assert json.loads((root / "current.json").read_text(encoding="utf-8")) == {
        "bundle_path": str(bundle),
        "schema_version": 2,
        "version_id": str(version),
    }
    runner = json.loads((bundle / "runner.json").read_text(encoding="utf-8"))
    assert runner["runtime_env_path"] == str(runtime_env.resolve())
    assert runner["private_key_path"] == str(bundle / "runner-private.key")
    assert runner["control_plane_public_key_path"] == str(bundle / "control-public.key")
    assert manifest["device_id"] == runner["device_id"]
    assert not {
        "control_private_key",
        "runner_private_key",
        "operator_token",
        "session_secret",
        "csrf_secret",
    }.intersection(manifest)
    assert "secret" in str(manifest["secret_bundle_path"]).casefold()
    assert "operator_token" not in json.dumps(manifest)

    secrets = load_control_secrets(
        bundle / "control-secrets.dpapi",
        unprotector=_unprotector,
    )
    assert {
        "control_private_key",
        "operator_token",
        "session_secret",
        "csrf_secret",
    }.issubset(secrets)
    assert secrets["schema_version"] == 2
    assert secrets["version_id"] == str(version)
    assert secrets["vercel_environment"] == "production"
    assert secrets["vercel_project_id"] == PROJECT_ID
    assert secrets["vercel_scope_id"] == SCOPE_ID
    assert all(
        len(str(secrets[name])) >= 43
        for name in ("control_private_key", "operator_token", "session_secret", "csrf_secret")
    )
    assert not any(
        str(secrets[name]).encode("ascii") in (bundle / "control-secrets.dpapi").read_bytes()
        for name in ("control_private_key", "operator_token", "session_secret", "csrf_secret")
    )
    if os.name != "nt":
        assert (bundle / "runner-private.key").stat().st_mode & 0o777 == 0o600

    with pytest.raises(IdentityProvisioningError, match="IDENTITY_VERSION_EXISTS"):
        create_identity_bundle(
            root=root.resolve(),
            repository_root=repository.resolve(),
            control_plane_url="https://control.example",
            runtime_env_path=runtime_env.resolve(),
            vercel_environment="production",
            vercel_project_id=PROJECT_ID,
            vercel_scope_id=SCOPE_ID,
            protector=_protector,
            version_id=version,
        )


def test_identity_publish_retries_transient_acl_denial_then_atomically_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / ".version.tmp"
    final = tmp_path / "version"
    staging.mkdir()
    (staging / "manifest.json").write_text("owned", encoding="utf-8")
    actual_replace = os.replace
    calls = 0
    delays: list[float] = []

    def transient_replace(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise PermissionError(errno.EACCES, "transient ACL denial")
        actual_replace(source, destination)

    monkeypatch.setattr(identity_module.os, "replace", transient_replace)
    monkeypatch.setattr(identity_module.time, "sleep", delays.append)

    identity_module._publish_identity_version(staging, final)

    assert calls == 3
    assert delays == [0.05, 0.05]
    assert not staging.exists()
    assert (final / "manifest.json").read_text(encoding="utf-8") == "owned"


def test_identity_publish_never_overwrites_competing_final_after_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / ".version.tmp"
    final = tmp_path / "version"
    staging.mkdir()
    (staging / "manifest.json").write_text("ours", encoding="utf-8")
    delays: list[float] = []

    def competing_replace(_source: Path, destination: Path) -> None:
        destination.mkdir()
        (destination / "manifest.json").write_text("winner", encoding="utf-8")
        raise PermissionError(errno.EACCES, "destination appeared")

    monkeypatch.setattr(identity_module.os, "replace", competing_replace)
    monkeypatch.setattr(identity_module.time, "sleep", delays.append)

    with pytest.raises(IdentityProvisioningError, match="IDENTITY_VERSION_EXISTS"):
        identity_module._publish_identity_version(staging, final)

    assert delays == []
    assert staging.is_dir()
    assert (final / "manifest.json").read_text(encoding="utf-8") == "winner"


def test_identity_publish_transient_retry_is_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staging = tmp_path / ".version.tmp"
    final = tmp_path / "version"
    staging.mkdir()
    calls = 0
    delays: list[float] = []

    def denied_replace(_source: Path, _destination: Path) -> None:
        nonlocal calls
        calls += 1
        raise PermissionError(errno.EACCES, "persistent ACL denial")

    monkeypatch.setattr(identity_module.os, "replace", denied_replace)
    monkeypatch.setattr(identity_module.time, "sleep", delays.append)

    with pytest.raises(PermissionError, match="persistent ACL denial"):
        identity_module._publish_identity_version(staging, final)

    assert calls == identity_module._IDENTITY_PUBLISH_ATTEMPTS
    assert delays == [identity_module._IDENTITY_PUBLISH_RETRY_SECONDS] * (
        identity_module._IDENTITY_PUBLISH_ATTEMPTS - 1
    )
    assert staging.is_dir()
    assert not final.exists()


def test_identity_root_rejects_repository_onedrive_unc_and_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(IdentityProvisioningError, match="IDENTITY_ROOT_IN_REPOSITORY"):
        validate_external_root(
            (repository / "private").resolve(),
            repository_root=repository.resolve(),
        )
    with pytest.raises(IdentityProvisioningError, match="IDENTITY_ROOT_IN_REPOSITORY"):
        validate_external_root(
            tmp_path.resolve(),
            repository_root=repository.resolve(),
        )

    onedrive = tmp_path / "OneDrive - Example"
    onedrive.mkdir()
    monkeypatch.setenv("OneDriveCommercial", str(onedrive.resolve()))
    with pytest.raises(IdentityProvisioningError, match="IDENTITY_ROOT_IN_ONEDRIVE"):
        validate_external_root(
            (onedrive / "JobApplyAgent").resolve(),
            repository_root=repository.resolve(),
        )
    with pytest.raises(IdentityProvisioningError, match="IDENTITY_ROOT_IN_ONEDRIVE"):
        validate_external_root(
            (tmp_path / "OneDrive - Unconfigured" / "JobApplyAgent").absolute(),
            repository_root=repository.resolve(),
        )

    with pytest.raises(
        IdentityProvisioningError,
        match="IDENTITY_ROOT_NOT_LOCAL_ABSOLUTE",
    ):
        validate_external_root(Path("relative"), repository_root=repository.resolve())

    if os.name == "nt":
        with pytest.raises(
            IdentityProvisioningError,
            match="IDENTITY_ROOT_NOT_LOCAL_ABSOLUTE",
        ):
            validate_external_root(
                Path(r"\\server\share\JobApplyAgent"),
                repository_root=repository.resolve(),
            )


def test_identity_rejects_raw_symlink_ancestor_before_resolution(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    target = tmp_path / "external-target"
    target.mkdir()
    link = tmp_path / "external-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(
        IdentityProvisioningError,
        match="IDENTITY_ROOT_REPARSE_POINT",
    ):
        validate_external_root(
            link / "JobApplyAgent" / "control-plane",
            repository_root=repository.resolve(),
        )


def test_identity_rejects_mapped_remote_drive_as_non_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapped_path = Path(r"Z:\JobApplyAgent\control-plane")
    kernel32 = SimpleNamespace(
        GetDriveTypeW=_FakeCFunction(lambda _root: 4),
    )
    monkeypatch.setattr(identity_module.os, "name", "nt")
    monkeypatch.setattr(
        identity_module.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )

    assert _is_local_fixed_ntfs_path(mapped_path) is False
    with pytest.raises(
        IdentityProvisioningError,
        match="IDENTITY_ROOT_NOT_LOCAL_ABSOLUTE",
    ):
        validate_external_root(
            mapped_path,
            repository_root=tmp_path.resolve(),
        )


@pytest.mark.parametrize(
    ("device_result", "last_error", "expected"),
    [(1, 0, True), (0, 4390, False), (0, 5, True)],
)
def test_windows_reparse_probe_uses_no_follow_handle_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    device_result: int,
    last_error: int,
    expected: bool,
) -> None:
    closed: list[object] = []
    kernel32 = SimpleNamespace(
        CreateFileW=_FakeCFunction(lambda *_arguments: 4242),
        DeviceIoControl=_FakeCFunction(lambda *_arguments: device_result),
        CloseHandle=_FakeCFunction(lambda handle: closed.append(handle) or 1),
        GetLastError=_FakeCFunction(lambda: last_error),
    )
    monkeypatch.setattr(identity_module.os, "name", "nt")
    monkeypatch.setattr(
        identity_module.ctypes,
        "windll",
        SimpleNamespace(kernel32=kernel32),
        raising=False,
    )

    assert _windows_path_is_reparse_point(tmp_path) is expected
    assert closed == [4242]


def test_vercel_cwd_rejects_onedrive_even_when_cloud_tag_is_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    onedrive = tmp_path / "OneDrive - Example"
    repository = onedrive / "repository"
    cwd = repository / "control_plane"
    link = cwd / ".vercel"
    link.mkdir(parents=True)
    (link / "project.json").write_text(
        json.dumps({"orgId": SCOPE_ID, "projectId": PROJECT_ID}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OneDriveCommercial", str(onedrive))

    with pytest.raises(IdentityProvisioningError, match="VERCEL_CWD_INVALID"):
        _validate_vercel_cwd(
            cwd.resolve(),
            repository_root=repository.resolve(),
            project_id=PROJECT_ID,
            scope_id=SCOPE_ID,
        )


def test_identity_rejects_relative_runtime_env_and_non_https_control_plane(
    tmp_path: Path,
):
    repository = tmp_path / "repository"
    repository.mkdir()
    root = (tmp_path / "identities").resolve()
    with pytest.raises(IdentityProvisioningError, match="RUNNER_ENV_PATH_NOT_ABSOLUTE"):
        create_identity_bundle(
            root=root,
            repository_root=repository.resolve(),
            control_plane_url="https://control.example",
            runtime_env_path=Path("runtime.env"),
            vercel_environment="production",
            vercel_project_id=PROJECT_ID,
            vercel_scope_id=SCOPE_ID,
            protector=_protector,
        )
    with pytest.raises(IdentityProvisioningError, match="CONTROL_PLANE_URL_INVALID"):
        create_identity_bundle(
            root=root,
            repository_root=repository.resolve(),
            control_plane_url="http://control.example",
            runtime_env_path=(tmp_path / "runtime-external" / "runtime.env").resolve(),
            vercel_environment="production",
            vercel_project_id=PROJECT_ID,
            vercel_scope_id=SCOPE_ID,
            protector=_protector,
        )


def test_selected_identity_strictly_binds_public_manifest_and_paths(tmp_path: Path) -> None:
    repository, root, runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)

    selected = load_selected_identity(root=root, repository_root=repository)

    assert selected.root == root
    assert selected.secret_bundle_path.name == "control-secrets.dpapi"
    assert selected.runner_public_key != selected.control_public_key
    runner = json.loads((selected.bundle_path / "runner.json").read_text(encoding="utf-8"))
    assert runner["runtime_env_path"] == str(runtime_env)

    manifest_path = selected.bundle_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = "not-allowed"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(IdentityProvisioningError, match="IDENTITY_MANIFEST_INVALID"):
        load_selected_identity(root=root, repository_root=repository)


def test_configure_vercel_dry_run_never_decrypts_or_invokes_cli(tmp_path: Path) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)

    def no_decrypt(_value: bytes) -> bytes:
        raise AssertionError("dry-run must not decrypt the bundle")

    def no_runner(
        _executable: Path,
        _arguments: object,
        _stdin: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        raise AssertionError("dry-run must not invoke Vercel")

    def no_version_reader(_executable: Path, _cwd: Path, _environment: object) -> str:
        raise AssertionError("dry-run must not invoke Vercel")

    def no_metadata_reader(
        _executable: Path,
        _arguments: object,
        _cwd: Path,
        _environment: object,
    ) -> str:
        raise AssertionError("dry-run must not invoke Vercel")

    result = configure_vercel_identity(
        root=root,
        repository_root=repository,
        vercel_cli=cli,
        vercel_cli_sha256=cli_digest,
        vercel_cli_version=CLI_VERSION,
        vercel_cwd=vercel_cwd,
        environment="production",
        project=PROJECT_ID,
        scope=SCOPE_ID,
        dry_run=True,
        runner=no_runner,
        metadata_reader=no_metadata_reader,
        version_reader=no_version_reader,
        unprotector=no_decrypt,
        platform_name="nt",
    )

    assert result.dry_run is True
    assert result.cli_mode == "native"
    assert result.configured_count == 0
    assert result.environment == "production"
    assert result.variable_names == (
        "CONTROL_OPERATOR_TOKEN",
        "CONTROL_SESSION_SECRET",
        "CONTROL_CSRF_SECRET",
        "CONTROL_SIGNING_PRIVATE_KEY_B64",
        "CONTROL_SIGNING_KEY_ID",
        "CONTROL_RUNNER_PUBLIC_KEY_B64",
        "CONTROL_RUNNER_DEVICE_ID",
        "CONTROL_IDENTITY_BUNDLE_DIGEST",
    )


def test_configure_vercel_streams_only_identity_values_over_stdin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    calls: list[tuple[Path, tuple[str, ...], str, Path, object]] = []
    metadata_calls: list[tuple[Path, tuple[str, ...], Path, object]] = []
    monkeypatch.setenv("NODE_OPTIONS", "--require=untrusted.js")
    monkeypatch.setenv("VERCEL_PROJECT_ID", "prj_wrongtarget123")
    monkeypatch.setenv("PATH", r"C:\untrusted")

    def runner(
        executable: Path,
        arguments: object,
        stdin_value: str,
        cwd: Path,
        environment: object,
    ) -> int:
        calls.append(
            (
                executable,
                tuple(str(value) for value in arguments),
                stdin_value,
                cwd,
                environment,
            )
        )
        return 0

    def metadata_reader(
        executable: Path,
        arguments: object,
        cwd: Path,
        environment: object,
    ) -> str:
        metadata_calls.append(
            (
                executable,
                tuple(str(value) for value in arguments),
                cwd,
                environment,
            )
        )
        return json.dumps(
            {
                "envs": [
                    {
                        "id": f"record_{index}",
                        "key": json.loads(call[2])["key"],
                        "type": "sensitive",
                        "target": ["production"],
                        "decrypted": False,
                        "value": "",
                    }
                    for index, call in enumerate(calls)
                ],
                "hiddenProductionEnvCount": 0,
            }
        )

    result = configure_vercel_identity(
        root=root,
        repository_root=repository,
        vercel_cli=cli,
        vercel_cli_sha256=cli_digest,
        vercel_cli_version=CLI_VERSION,
        vercel_cwd=vercel_cwd,
        environment="production",
        project=PROJECT_ID,
        scope=SCOPE_ID,
        runner=runner,
        metadata_reader=metadata_reader,
        version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
        unprotector=_unprotector,
        platform_name="nt",
    )

    assert result.configured_count == 8
    assert len(calls) == 8
    assert len(metadata_calls) == 2
    metadata_executable, metadata_arguments, metadata_cwd, metadata_environment = metadata_calls[0]
    assert metadata_executable == cli
    assert metadata_cwd == vercel_cwd
    assert metadata_arguments[:2] == (
        "api",
        f"/v10/projects/{PROJECT_ID}/env?decrypt=false",
    )
    assert "--raw" in metadata_arguments
    assert metadata_arguments[metadata_arguments.index("--scope") + 1] == SCOPE_ID
    assert "PATH" not in metadata_environment
    assert "NODE_OPTIONS" not in metadata_environment
    selected = load_selected_identity(root=root, repository_root=repository)
    protected = load_control_secrets(
        selected.secret_bundle_path,
        unprotector=_unprotector,
    )
    expected_values = {
        protected["operator_token"],
        protected["session_secret"],
        protected["csrf_secret"],
        protected["control_private_key"],
        str(selected.control_signing_key_id),
        selected.runner_public_key,
        str(selected.device_id),
    }
    request_bodies = [json.loads(call[2]) for call in calls]
    assert {body["value"] for body in request_bodies[:-1]} == expected_values
    assert request_bodies[-1]["key"] == "CONTROL_IDENTITY_BUNDLE_DIGEST"
    assert request_bodies[-1]["value"].startswith("v2:")
    configured = {body["key"]: body["value"] for body in request_bodies}
    assert configured["CONTROL_IDENTITY_BUNDLE_DIGEST"] == build_identity_bundle_digest(
        configured,
        version_id=selected.version_id,
        environment="production",
        project_id=PROJECT_ID,
        scope_id=SCOPE_ID,
    )
    for executable, arguments, stdin_value, cwd, environment in calls:
        assert executable == cli
        assert cwd == vercel_cwd
        assert arguments[:2] == ("api", f"/v10/projects/{PROJECT_ID}/env")
        assert arguments[arguments.index("--method") + 1] == "POST"
        assert arguments[arguments.index("--input") + 1] == "-"
        assert "--silent" in arguments
        assert "--force" not in arguments
        assert arguments[arguments.index("--scope") + 1] == SCOPE_ID
        assert arguments[arguments.index("--cwd") + 1] == str(vercel_cwd)
        assert "PATH" not in environment
        assert "NODE_OPTIONS" not in environment
        body = json.loads(stdin_value)
        assert body["type"] == "sensitive"
        assert body["target"] == ["production"]
        assert stdin_value not in "\0".join(arguments)
        assert stdin_value not in repr(result)


def test_configure_vercel_creates_preview_records_without_upserting_production(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(
        tmp_path,
        vercel_environment="preview",
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def metadata_reader(
        _executable: Path,
        _arguments: object,
        _cwd: Path,
        _environment: object,
    ) -> str:
        created = [
            {
                "id": f"preview_record_{index}",
                "key": body["key"],
                "type": "sensitive",
                "target": ["preview"],
                "decrypted": False,
                "value": "",
            }
            for index, (_arguments, body) in enumerate(calls)
        ]
        return json.dumps(
            {
                "envs": [
                    {
                        "id": "production_record",
                        "key": "CONTROL_OPERATOR_TOKEN",
                        "type": "sensitive",
                        "target": ["production"],
                        "decrypted": False,
                        "value": "",
                    }
                ]
                + created,
                "hiddenProductionEnvCount": 0,
            }
        )

    def runner(
        _executable: Path,
        arguments: object,
        stdin_value: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        calls.append(
            (
                tuple(str(value) for value in arguments),
                json.loads(stdin_value),
            )
        )
        return 0

    result = configure_vercel_identity(
        root=root,
        repository_root=repository,
        vercel_cli=cli,
        vercel_cli_sha256=cli_digest,
        vercel_cli_version=CLI_VERSION,
        vercel_cwd=vercel_cwd,
        environment="preview",
        project=PROJECT_ID,
        scope=SCOPE_ID,
        runner=runner,
        metadata_reader=metadata_reader,
        version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
        unprotector=_unprotector,
        platform_name="nt",
    )

    assert result.configured_count == 8
    assert len(calls) == 8
    for arguments, body in calls:
        assert arguments[:2] == ("api", f"/v10/projects/{PROJECT_ID}/env")
        assert arguments[arguments.index("--method") + 1] == "POST"
        assert "upsert" not in "\0".join(arguments)
        assert "production_record" not in "\0".join(arguments)
        assert body["target"] == ["preview"]


def test_configure_vercel_post_write_attestation_rejects_other_scope_collapse(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(
        tmp_path,
        vercel_environment="preview",
    )
    names = tuple(identity_module._IDENTITY_DERIVED_VERCEL_VARIABLES)
    calls: list[dict[str, object]] = []
    metadata_reads = 0

    def metadata_reader(
        _executable: Path,
        _arguments: object,
        _cwd: Path,
        _environment: object,
    ) -> str:
        nonlocal metadata_reads
        metadata_reads += 1
        target = "production" if metadata_reads == 1 else "preview"
        records = [
            {
                "id": f"{target}_{index}",
                "key": name,
                "type": "sensitive",
                "target": [target],
                "decrypted": False,
                "value": "",
            }
            for index, name in enumerate(names)
        ]
        return json.dumps(
            {
                "envs": records,
                "hiddenProductionEnvCount": 0,
            }
        )

    def runner(
        _executable: Path,
        _arguments: object,
        stdin_value: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        calls.append(json.loads(stdin_value))
        return 0

    with pytest.raises(IdentityProvisioningError, match="VERCEL_ENV_ATTESTATION_FAILED"):
        configure_vercel_identity(
            root=root,
            repository_root=repository,
            vercel_cli=cli,
            vercel_cli_sha256=cli_digest,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=vercel_cwd,
            environment="preview",
            project=PROJECT_ID,
            scope=SCOPE_ID,
            runner=runner,
            metadata_reader=metadata_reader,
            version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
            unprotector=_unprotector,
            platform_name="nt",
        )

    assert metadata_reads == 2
    assert len(calls) == 8
    assert calls[-1]["key"] == "CONTROL_IDENTITY_BUNDLE_DIGEST"


def test_configure_vercel_patches_only_exact_environment_record_ids(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    names = (
        "CONTROL_OPERATOR_TOKEN",
        "CONTROL_SESSION_SECRET",
        "CONTROL_CSRF_SECRET",
        "CONTROL_SIGNING_PRIVATE_KEY_B64",
        "CONTROL_SIGNING_KEY_ID",
        "CONTROL_RUNNER_PUBLIC_KEY_B64",
        "CONTROL_RUNNER_DEVICE_ID",
        "CONTROL_IDENTITY_BUNDLE_DIGEST",
    )
    identifiers = {name: f"record_{index}" for index, name in enumerate(names)}
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def metadata_reader(
        _executable: Path,
        _arguments: object,
        _cwd: Path,
        _environment: object,
    ) -> str:
        return json.dumps(
            {
                "envs": [
                    {
                        "id": identifier,
                        "key": name,
                        "type": "sensitive",
                        "target": ["production"],
                        "decrypted": False,
                        "value": "",
                    }
                    for name, identifier in identifiers.items()
                ],
                "hiddenProductionEnvCount": 0,
            }
        )

    def runner(
        _executable: Path,
        arguments: object,
        stdin_value: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        calls.append(
            (
                tuple(str(value) for value in arguments),
                json.loads(stdin_value),
            )
        )
        return 0

    configure_vercel_identity(
        root=root,
        repository_root=repository,
        vercel_cli=cli,
        vercel_cli_sha256=cli_digest,
        vercel_cli_version=CLI_VERSION,
        vercel_cwd=vercel_cwd,
        environment="production",
        project=PROJECT_ID,
        scope=SCOPE_ID,
        runner=runner,
        metadata_reader=metadata_reader,
        version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
        unprotector=_unprotector,
        platform_name="nt",
    )

    assert len(calls) == 8
    for name, (arguments, body) in zip(names, calls, strict=True):
        assert arguments[:2] == (
            "api",
            f"/v9/projects/{PROJECT_ID}/env/{identifiers[name]}",
        )
        assert arguments[arguments.index("--method") + 1] == "PATCH"
        assert "key" not in body
        assert body["target"] == ["production"]


@pytest.mark.parametrize("environment", ["preview", "production"])
def test_configure_vercel_selects_only_the_exact_record_when_both_scopes_exist(
    tmp_path: Path,
    environment: str,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(
        tmp_path,
        vercel_environment=environment,
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    records = [
        {
            "id": f"{target}_operator",
            "key": "CONTROL_OPERATOR_TOKEN",
            "type": "sensitive",
            "target": [target],
            "decrypted": False,
            "value": "",
        }
        for target in ("preview", "production")
    ]

    def runner(
        _executable: Path,
        arguments: object,
        stdin_value: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        calls.append(
            (
                tuple(str(value) for value in arguments),
                json.loads(stdin_value),
            )
        )
        return 0

    def metadata_reader(
        _executable: Path,
        _arguments: object,
        _cwd: Path,
        _environment: object,
    ) -> str:
        created = [
            {
                "id": f"{environment}_created_{index}",
                "key": body["key"],
                "type": "sensitive",
                "target": [environment],
                "decrypted": False,
                "value": "",
            }
            for index, (_arguments, body) in enumerate(calls)
            if "key" in body
        ]
        return json.dumps(
            {
                "envs": records + created,
                "hiddenProductionEnvCount": 0,
            }
        )

    configure_vercel_identity(
        root=root,
        repository_root=repository,
        vercel_cli=cli,
        vercel_cli_sha256=cli_digest,
        vercel_cli_version=CLI_VERSION,
        vercel_cwd=vercel_cwd,
        environment=environment,
        project=PROJECT_ID,
        scope=SCOPE_ID,
        runner=runner,
        metadata_reader=metadata_reader,
        version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
        unprotector=_unprotector,
        platform_name="nt",
    )

    operator_arguments, operator_body = calls[0]
    assert operator_arguments[:2] == (
        "api",
        f"/v9/projects/{PROJECT_ID}/env/{environment}_operator",
    )
    assert operator_arguments[operator_arguments.index("--method") + 1] == "PATCH"
    assert "key" not in operator_body
    assert operator_body["target"] == [environment]
    assert f"{'production' if environment == 'preview' else 'preview'}_operator" not in (
        "\0".join(argument for arguments, _body in calls for argument in arguments)
    )


@pytest.mark.parametrize(
    "record,reason",
    [
        (
            {
                "id": "combined_target",
                "key": "CONTROL_OPERATOR_TOKEN",
                "type": "sensitive",
                "target": ["preview", "production"],
                "decrypted": False,
                "value": "",
            },
            "VERCEL_ENV_TARGET_AMBIGUOUS_CONTROL_OPERATOR_TOKEN",
        ),
        (
            {
                "id": "decrypted_record",
                "key": "CONTROL_OPERATOR_TOKEN",
                "type": "sensitive",
                "target": ["preview"],
                "decrypted": True,
                "value": "must-not-be-read",
            },
            "VERCEL_ENV_METADATA_UNSAFE",
        ),
        *(
            (
                {
                    "id": f"malformed_{field}",
                    "key": "CONTROL_OPERATOR_TOKEN",
                    "type": "sensitive",
                    "target": ["preview"],
                    "decrypted": False,
                    "value": "",
                    field: malformed_value,
                },
                "VERCEL_ENV_METADATA_INVALID",
            )
            for field, malformed_value in (
                ("value", []),
                ("legacyValue", {}),
                ("vsmValue", []),
                ("gitBranch", {}),
            )
        ),
    ],
)
def test_configure_vercel_rejects_ambiguous_or_decrypted_metadata_before_decrypt(
    tmp_path: Path,
    record: dict[str, object],
    reason: str,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(
        tmp_path,
        vercel_environment="preview",
    )
    decrypted = False

    def no_decrypt(_value: bytes) -> bytes:
        nonlocal decrypted
        decrypted = True
        raise AssertionError

    with pytest.raises(IdentityProvisioningError, match=reason):
        configure_vercel_identity(
            root=root,
            repository_root=repository,
            vercel_cli=cli,
            vercel_cli_sha256=cli_digest,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=vercel_cwd,
            environment="preview",
            project=PROJECT_ID,
            scope=SCOPE_ID,
            runner=lambda *_arguments: 0,
            metadata_reader=lambda *_arguments: json.dumps(
                {
                    "envs": [record],
                    "hiddenProductionEnvCount": 0,
                }
            ),
            version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
            unprotector=no_decrypt,
            platform_name="nt",
        )
    assert decrypted is False


@pytest.mark.parametrize(
    "environment,payload,reason",
    [
        (
            "production",
            {"envs": [], "hiddenProductionEnvCount": 1},
            "VERCEL_ENV_METADATA_INCOMPLETE",
        ),
        (
            "production",
            {"envs": [], "hiddenProductionEnvCount": True},
            "VERCEL_ENV_METADATA_INVALID",
        ),
        (
            "preview",
            {
                "envs": [],
                "pagination": {"count": 0, "next": 123, "prev": None},
            },
            "VERCEL_ENV_METADATA_INCOMPLETE",
        ),
        (
            "preview",
            {
                "envs": [],
                "pagination": {"count": 1, "next": None, "prev": None},
            },
            "VERCEL_ENV_METADATA_INCOMPLETE",
        ),
        (
            "preview",
            {"envs": []},
            "VERCEL_ENV_METADATA_INCOMPLETE",
        ),
        (
            "preview",
            {
                "envs": [
                    {
                        "id": "same_record",
                        "key": name,
                        "type": "sensitive",
                        "target": ["preview"],
                        "decrypted": False,
                        "value": "",
                    }
                    for name in ("CONTROL_OPERATOR_TOKEN", "CONTROL_SESSION_SECRET")
                ],
                "hiddenProductionEnvCount": 0,
            },
            "VERCEL_ENV_RECORD_ID_ALIAS",
        ),
        (
            "preview",
            {
                "envs": [
                    {
                        "id": "shared_record",
                        "key": "UNRELATED_VARIABLE",
                    },
                    {
                        "id": "shared_record",
                        "key": "CONTROL_OPERATOR_TOKEN",
                        "type": "sensitive",
                        "target": ["preview"],
                        "decrypted": False,
                        "value": "",
                    },
                ],
                "hiddenProductionEnvCount": 0,
            },
            "VERCEL_ENV_RECORD_ID_ALIAS",
        ),
        (
            "preview",
            {
                "envs": [
                    {
                        "id": identifier,
                        "key": "CONTROL_OPERATOR_TOKEN",
                        "type": "sensitive",
                        "target": ["preview"],
                        "decrypted": False,
                        "value": "",
                    }
                    for identifier in ("duplicate_one", "duplicate_two")
                ],
                "hiddenProductionEnvCount": 0,
            },
            "VERCEL_ENV_TARGET_AMBIGUOUS_CONTROL_OPERATOR_TOKEN",
        ),
    ],
)
def test_configure_vercel_rejects_incomplete_or_aliasing_metadata_before_decrypt(
    tmp_path: Path,
    environment: str,
    payload: dict[str, object],
    reason: str,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(
        tmp_path,
        vercel_environment=environment,
    )
    decrypted = False

    def no_decrypt(_value: bytes) -> bytes:
        nonlocal decrypted
        decrypted = True
        raise AssertionError

    with pytest.raises(IdentityProvisioningError, match=reason):
        configure_vercel_identity(
            root=root,
            repository_root=repository,
            vercel_cli=cli,
            vercel_cli_sha256=cli_digest,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=vercel_cwd,
            environment=environment,
            project=PROJECT_ID,
            scope=SCOPE_ID,
            runner=lambda *_arguments: 0,
            metadata_reader=lambda *_arguments: json.dumps(payload),
            version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
            unprotector=no_decrypt,
            platform_name="nt",
        )
    assert decrypted is False


def test_configure_vercel_rejects_duplicate_metadata_json_keys_before_decrypt(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(
        tmp_path,
        vercel_environment="preview",
    )
    decrypted = False

    def no_decrypt(_value: bytes) -> bytes:
        nonlocal decrypted
        decrypted = True
        raise AssertionError

    with pytest.raises(IdentityProvisioningError, match="VERCEL_ENV_METADATA_INVALID"):
        configure_vercel_identity(
            root=root,
            repository_root=repository,
            vercel_cli=cli,
            vercel_cli_sha256=cli_digest,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=vercel_cwd,
            environment="preview",
            project=PROJECT_ID,
            scope=SCOPE_ID,
            runner=lambda *_arguments: 0,
            metadata_reader=lambda *_arguments: (
                '{"envs":[],"envs":[],"hiddenProductionEnvCount":0}'
            ),
            version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
            unprotector=no_decrypt,
            platform_name="nt",
        )
    assert decrypted is False


def test_configure_vercel_node_js_mode_pins_both_files_and_uses_direct_command_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, root, _runtime_env, _cli, vercel_cwd, _cli_digest = _test_identity(tmp_path)
    node, node_digest, entrypoint, entrypoint_digest = _test_node_cli(tmp_path)
    calls: list[tuple[Path, tuple[str, ...], Path, object]] = []
    written_names: list[str] = []
    monkeypatch.setenv("NODE_OPTIONS", "--require=untrusted.js")
    monkeypatch.setenv("PATH", r"C:\untrusted")

    def runner(
        executable: Path,
        arguments: object,
        stdin_value: str,
        cwd: Path,
        environment: object,
    ) -> int:
        written_names.append(str(json.loads(stdin_value)["key"]))
        calls.append(
            (
                executable,
                tuple(str(value) for value in arguments),
                cwd,
                environment,
            )
        )
        return 0

    def metadata_reader(
        _executable: Path,
        _arguments: object,
        _cwd: Path,
        _environment: object,
    ) -> str:
        return json.dumps(
            {
                "envs": [
                    {
                        "id": f"record_{index}",
                        "key": name,
                        "type": "sensitive",
                        "target": ["production"],
                        "decrypted": False,
                        "value": "",
                    }
                    for index, name in enumerate(written_names)
                ],
                "hiddenProductionEnvCount": 0,
            }
        )

    result = configure_vercel_identity(
        root=root,
        repository_root=repository,
        vercel_node=node,
        vercel_node_sha256=node_digest,
        vercel_js_entrypoint=entrypoint,
        vercel_js_entrypoint_sha256=entrypoint_digest,
        vercel_cli_version=CLI_VERSION,
        vercel_cwd=vercel_cwd,
        environment="production",
        project=PROJECT_ID,
        scope=SCOPE_ID,
        runner=runner,
        metadata_reader=metadata_reader,
        version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
        unprotector=_unprotector,
        platform_name="nt",
    )

    assert result.cli_mode == "node_js"
    assert result.configured_count == 8
    assert len(calls) == 8
    for executable, arguments, cwd, environment in calls:
        assert executable == node
        assert arguments[0] == str(entrypoint)
        assert arguments[1:3] == (
            "api",
            f"/v10/projects/{PROJECT_ID}/env",
        )
        assert cwd == vercel_cwd
        assert "PATH" not in environment
        assert "NODE_OPTIONS" not in environment


def test_native_mode_accepts_official_package_internal_executable_layout(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, _cli, vercel_cwd, _cli_digest = _test_identity(tmp_path)
    native = tmp_path / "npm" / "node_modules" / "@vercel" / "vc-native" / "bin" / "vercel.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"official-package-internal-native-layout")
    digest = hashlib.sha256(native.read_bytes()).hexdigest()

    result = configure_vercel_identity(
        root=root,
        repository_root=repository,
        vercel_cli=native.resolve(),
        vercel_cli_sha256=digest,
        vercel_cli_version=CLI_VERSION,
        vercel_cwd=vercel_cwd,
        environment="production",
        project=PROJECT_ID,
        scope=SCOPE_ID,
        dry_run=True,
        platform_name="nt",
    )

    assert result.cli_mode == "native"
    assert result.configured_count == 0


def test_node_js_default_subprocess_shape_is_node_then_absolute_entrypoint(
    tmp_path: Path,
) -> None:
    node, _node_digest, entrypoint, _entrypoint_digest = _test_node_cli(tmp_path)
    working_directory = tmp_path.resolve()
    observed: list[object] = []

    def fake_run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.append((command, kwargs))
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="Vercel CLI 58.1.0",
            stderr="",
        )

    with patch("scripts.control_plane_identity.subprocess.run", side_effect=fake_run):
        assert (
            _read_vercel_cli_version(
                node,
                working_directory,
                {"CI": "1", "NO_COLOR": "1"},
                prefix_arguments=(str(entrypoint),),
            )
            == CLI_VERSION
        )
        assert (
            _run_vercel_command(
                node,
                (str(entrypoint), "env", "add", "CONTROL_OPERATOR_TOKEN"),
                "secret-over-stdin",
                working_directory,
                {"CI": "1", "NO_COLOR": "1"},
            )
            == 0
        )

    version_command, version_kwargs = observed[0]
    assert version_command == [str(node), str(entrypoint), "--version"]
    assert version_kwargs["shell"] is False
    command, command_kwargs = observed[1]
    assert command == [
        str(node),
        str(entrypoint),
        "env",
        "add",
        "CONTROL_OPERATOR_TOKEN",
    ]
    assert command_kwargs["input"] == "secret-over-stdin"
    assert command_kwargs["shell"] is False
    assert command_kwargs["stdout"] is subprocess.DEVNULL
    assert command_kwargs["stderr"] is subprocess.DEVNULL


def test_vercel_metadata_reader_captures_only_bounded_stdout(
    tmp_path: Path,
) -> None:
    executable = (tmp_path / "node.exe").resolve()
    working_directory = tmp_path.resolve()
    observed: list[object] = []
    metadata = json.dumps(
        {
            "envs": [],
            "hiddenProductionEnvCount": 0,
        }
    )

    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = io.BytesIO(metadata.encode("utf-8"))

        def poll(self) -> int:
            return 0

        def wait(self, timeout: int | None = None) -> int:
            assert timeout in {5, 120}
            return 0

        def kill(self) -> None:
            raise AssertionError("bounded metadata must not be killed")

    def fake_popen(command: object, **kwargs: object) -> FakeProcess:
        observed.append((command, kwargs))
        return FakeProcess()

    arguments = (
        r"C:\pinned\vercel\dist\vc.js",
        "api",
        f"/v10/projects/{PROJECT_ID}/env?decrypt=false",
        "--raw",
    )
    with patch("scripts.control_plane_identity.subprocess.Popen", side_effect=fake_popen):
        assert (
            identity_module._read_vercel_environment_metadata(
                executable,
                arguments,
                working_directory,
                {"CI": "1", "NO_COLOR": "1"},
            )
            == metadata
        )

    command, kwargs = observed[0]
    assert command == [str(executable), *arguments]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert "text" not in kwargs


def test_vercel_metadata_reader_stops_before_buffering_unbounded_stdout(
    tmp_path: Path,
) -> None:
    output_bytes = identity_module._MAX_VERCEL_METADATA_BYTES + 1

    with pytest.raises(IdentityProvisioningError, match="VERCEL_ENV_METADATA_TOO_LARGE"):
        identity_module._read_vercel_environment_metadata(
            Path(identity_module.sys.executable).resolve(),
            (
                "-c",
                f"import sys; sys.stdout.buffer.write(b'x' * {output_bytes})",
            ),
            tmp_path.resolve(),
            os.environ,
        )


def test_sanitized_vercel_environment_uses_only_explicit_pinned_ca(tmp_path: Path) -> None:
    certificate, digest = _test_ca_certificate(tmp_path)
    trust = identity_module._select_vercel_ca_trust(certificate, digest)
    source = {
        "APPDATA": str(tmp_path / "appdata"),
        "NODE_EXTRA_CA_CERTS": str(tmp_path / "ambient-untrusted.pem"),
        "PATH": "must-not-leak",
    }

    without_trust = identity_module._sanitized_vercel_environment(source)
    with_trust = identity_module._sanitized_vercel_environment(source, ca_trust=trust)

    assert "NODE_EXTRA_CA_CERTS" not in without_trust
    assert "PATH" not in with_trust
    assert with_trust["NODE_EXTRA_CA_CERTS"] == str(certificate)
    assert with_trust["APPDATA"] == source["APPDATA"]


def test_configure_vercel_rejects_partial_ca_trust_configuration(tmp_path: Path) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    certificate, digest = _test_ca_certificate(tmp_path)
    common = {
        "root": root,
        "repository_root": repository,
        "vercel_cli": cli,
        "vercel_cli_sha256": cli_digest,
        "vercel_cli_version": CLI_VERSION,
        "vercel_cwd": vercel_cwd,
        "environment": "production",
        "project": PROJECT_ID,
        "scope": SCOPE_ID,
        "dry_run": True,
        "platform_name": "nt",
    }

    with pytest.raises(IdentityProvisioningError, match="VERCEL_CA_TRUST_MODE_INVALID"):
        configure_vercel_identity(**common, vercel_ca_certificate=certificate)
    with pytest.raises(IdentityProvisioningError, match="VERCEL_CA_TRUST_MODE_INVALID"):
        configure_vercel_identity(**common, vercel_ca_certificate_sha256=digest)
    with pytest.raises(
        IdentityProvisioningError,
        match="VERCEL_CA_CERTIFICATE_DIGEST_MISMATCH",
    ):
        configure_vercel_identity(
            **common,
            vercel_ca_certificate=certificate,
            vercel_ca_certificate_sha256="0" * 64,
        )


def test_configure_vercel_rehashes_enterprise_ca_before_each_write(tmp_path: Path) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    certificate, digest = _test_ca_certificate(tmp_path)
    calls = 0
    seen_environments: list[dict[str, str]] = []

    def runner(
        _executable: Path,
        _arguments: object,
        _stdin_value: str,
        _cwd: Path,
        environment: object,
    ) -> int:
        nonlocal calls
        calls += 1
        seen_environments.append(dict(environment))  # type: ignore[arg-type]
        certificate.write_bytes(certificate.read_bytes() + b"\n")
        return 0

    with pytest.raises(
        IdentityProvisioningError,
        match="VERCEL_CA_CERTIFICATE_DIGEST_MISMATCH",
    ):
        configure_vercel_identity(
            root=root,
            repository_root=repository,
            vercel_cli=cli,
            vercel_cli_sha256=cli_digest,
            vercel_ca_certificate=certificate,
            vercel_ca_certificate_sha256=digest,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=vercel_cwd,
            environment="production",
            project=PROJECT_ID,
            scope=SCOPE_ID,
            runner=runner,
            metadata_reader=_empty_vercel_metadata,
            version_reader=lambda _executable, _cwd, environment: (
                CLI_VERSION
                if environment.get("NODE_EXTRA_CA_CERTS") == str(certificate)
                else "missing-ca"
            ),
            unprotector=_unprotector,
            platform_name="nt",
        )
    assert calls == 1
    assert seen_environments[0]["NODE_EXTRA_CA_CERTS"] == str(certificate)


def test_configure_vercel_node_js_rehashes_entrypoint_before_each_write(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, _cli, vercel_cwd, _cli_digest = _test_identity(tmp_path)
    node, node_digest, entrypoint, entrypoint_digest = _test_node_cli(tmp_path)
    calls = 0

    def runner(
        _executable: Path,
        _arguments: object,
        _stdin_value: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        nonlocal calls
        calls += 1
        entrypoint.write_bytes(b"changed-after-first-write")
        return 0

    with pytest.raises(
        IdentityProvisioningError,
        match="VERCEL_JS_ENTRYPOINT_DIGEST_MISMATCH",
    ):
        configure_vercel_identity(
            root=root,
            repository_root=repository,
            vercel_node=node,
            vercel_node_sha256=node_digest,
            vercel_js_entrypoint=entrypoint,
            vercel_js_entrypoint_sha256=entrypoint_digest,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=vercel_cwd,
            environment="production",
            project=PROJECT_ID,
            scope=SCOPE_ID,
            runner=runner,
            metadata_reader=_empty_vercel_metadata,
            version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
            unprotector=_unprotector,
            platform_name="nt",
        )
    assert calls == 1


def test_configure_vercel_rejects_partial_or_ambiguous_cli_modes(tmp_path: Path) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    node, node_digest, entrypoint, entrypoint_digest = _test_node_cli(tmp_path)
    common = {
        "root": root,
        "repository_root": repository,
        "vercel_cli_version": CLI_VERSION,
        "vercel_cwd": vercel_cwd,
        "environment": "production",
        "project": PROJECT_ID,
        "scope": SCOPE_ID,
        "dry_run": True,
        "platform_name": "nt",
    }

    with pytest.raises(IdentityProvisioningError, match="VERCEL_CLI_MODE_INVALID"):
        configure_vercel_identity(**common)
    with pytest.raises(IdentityProvisioningError, match="VERCEL_CLI_MODE_INVALID"):
        configure_vercel_identity(
            **common,
            vercel_node=node,
            vercel_node_sha256=node_digest,
        )
    with pytest.raises(IdentityProvisioningError, match="VERCEL_CLI_MODE_INVALID"):
        configure_vercel_identity(
            **common,
            vercel_cli=cli,
            vercel_cli_sha256=cli_digest,
            vercel_node=node,
            vercel_node_sha256=node_digest,
            vercel_js_entrypoint=entrypoint,
            vercel_js_entrypoint_sha256=entrypoint_digest,
        )


def test_configure_vercel_failure_is_stable_and_never_contains_value(tmp_path: Path) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    seen_values: list[str] = []

    def runner(
        _executable: Path,
        arguments: object,
        stdin_value: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        seen_values.append(stdin_value)
        body = json.loads(stdin_value)
        return 9 if body["key"] == "CONTROL_SESSION_SECRET" else 0

    with pytest.raises(
        IdentityProvisioningError,
        match="VERCEL_ENV_CONFIGURATION_FAILED_CONTROL_SESSION_SECRET",
    ) as captured:
        configure_vercel_identity(
            root=root,
            repository_root=repository,
            vercel_cli=cli,
            vercel_cli_sha256=cli_digest,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=vercel_cwd,
            environment="production",
            project=PROJECT_ID,
            scope=SCOPE_ID,
            runner=runner,
            metadata_reader=_empty_vercel_metadata,
            version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
            unprotector=_unprotector,
            platform_name="nt",
        )
    assert all(value not in str(captured.value) for value in seen_values)


def test_configure_vercel_partial_write_never_publishes_digest_and_retry_completes(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    names = (
        "CONTROL_OPERATOR_TOKEN",
        "CONTROL_SESSION_SECRET",
        "CONTROL_CSRF_SECRET",
        "CONTROL_SIGNING_PRIVATE_KEY_B64",
        "CONTROL_SIGNING_KEY_ID",
        "CONTROL_RUNNER_PUBLIC_KEY_B64",
        "CONTROL_RUNNER_DEVICE_ID",
        "CONTROL_IDENTITY_BUNDLE_DIGEST",
    )
    remote = {
        name: {
            "id": f"record_{index}",
            "value": f"old:{name}",
        }
        for index, name in enumerate(names)
    }
    old_digest = "v2:" + ("0" * 64)
    remote["CONTROL_IDENTITY_BUNDLE_DIGEST"]["value"] = old_digest
    write_order: list[str] = []
    fail_session_after_apply = True

    def metadata_reader(
        _executable: Path,
        _arguments: object,
        _cwd: Path,
        _environment: object,
    ) -> str:
        return json.dumps(
            {
                "envs": [
                    {
                        "id": record["id"],
                        "key": name,
                        "type": "sensitive",
                        "target": ["production"],
                        "decrypted": False,
                        "value": "",
                    }
                    for name, record in remote.items()
                ],
                "hiddenProductionEnvCount": 0,
            }
        )

    def runner(
        _executable: Path,
        arguments: object,
        stdin_value: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        nonlocal fail_session_after_apply
        command = tuple(str(value) for value in arguments)
        body = json.loads(stdin_value)
        assert command[command.index("--method") + 1] == "PATCH"
        assert "key" not in body
        record_id = command[1].rsplit("/", 1)[-1]
        name = next(name for name, record in remote.items() if record["id"] == record_id)
        remote[name]["value"] = body["value"]
        write_order.append(name)
        if name == "CONTROL_SESSION_SECRET" and fail_session_after_apply:
            fail_session_after_apply = False
            return 9
        return 0

    arguments = {
        "root": root,
        "repository_root": repository,
        "vercel_cli": cli,
        "vercel_cli_sha256": cli_digest,
        "vercel_cli_version": CLI_VERSION,
        "vercel_cwd": vercel_cwd,
        "environment": "production",
        "project": PROJECT_ID,
        "scope": SCOPE_ID,
        "runner": runner,
        "metadata_reader": metadata_reader,
        "version_reader": lambda _executable, _cwd, _environment: CLI_VERSION,
        "unprotector": _unprotector,
        "platform_name": "nt",
    }
    with pytest.raises(
        IdentityProvisioningError,
        match="VERCEL_ENV_CONFIGURATION_FAILED_CONTROL_SESSION_SECRET",
    ):
        configure_vercel_identity(**arguments)
    assert write_order == ["CONTROL_OPERATOR_TOKEN", "CONTROL_SESSION_SECRET"]
    assert remote["CONTROL_OPERATOR_TOKEN"]["value"] != "old:CONTROL_OPERATOR_TOKEN"
    assert remote["CONTROL_SESSION_SECRET"]["value"] != "old:CONTROL_SESSION_SECRET"
    assert remote["CONTROL_IDENTITY_BUNDLE_DIGEST"]["value"] == old_digest

    write_order.clear()
    result = configure_vercel_identity(**arguments)
    assert result.configured_count == 8
    assert write_order[-1] == "CONTROL_IDENTITY_BUNDLE_DIGEST"
    assert remote["CONTROL_IDENTITY_BUNDLE_DIGEST"]["value"] != old_digest
    assert str(remote["CONTROL_IDENTITY_BUNDLE_DIGEST"]["value"]).startswith("v2:")


def test_configure_vercel_create_applied_before_error_retries_with_exact_patch(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(
        tmp_path,
        vercel_environment="preview",
    )
    names = tuple(identity_module._IDENTITY_DERIVED_VERCEL_VARIABLES)
    production_ids = {name: f"production_record_{index}" for index, name in enumerate(names)}
    preview: dict[str, dict[str, str]] = {}
    write_calls: list[tuple[str, str, str, dict[str, object]]] = []
    fail_operator_after_apply = True

    def metadata_reader(
        _executable: Path,
        _arguments: object,
        _cwd: Path,
        _environment: object,
    ) -> str:
        production_records = [
            {
                "id": identifier,
                "key": name,
                "type": "sensitive",
                "target": ["production"],
                "decrypted": False,
                "value": "",
            }
            for name, identifier in production_ids.items()
        ]
        preview_records = [
            {
                "id": record["id"],
                "key": name,
                "type": "sensitive",
                "target": ["preview"],
                "decrypted": False,
                "value": "",
            }
            for name, record in preview.items()
        ]
        return json.dumps(
            {
                "envs": production_records + preview_records,
                "hiddenProductionEnvCount": 0,
            }
        )

    def runner(
        _executable: Path,
        arguments: object,
        stdin_value: str,
        _cwd: Path,
        _environment: object,
    ) -> int:
        nonlocal fail_operator_after_apply
        command = tuple(str(value) for value in arguments)
        method = command[command.index("--method") + 1]
        body = json.loads(stdin_value)
        endpoint = command[1]
        if method == "POST":
            name = str(body["key"])
            assert name not in preview
            record_id = (
                "preview_created_operator"
                if name == "CONTROL_OPERATOR_TOKEN"
                else f"preview_created_{len(preview)}"
            )
            preview[name] = {"id": record_id, "value": str(body["value"])}
        else:
            assert method == "PATCH"
            assert "key" not in body
            record_id = endpoint.rsplit("/", 1)[-1]
            name = next(name for name, record in preview.items() if record["id"] == record_id)
            preview[name]["value"] = str(body["value"])
        write_calls.append((method, name, endpoint, body))
        if name == "CONTROL_OPERATOR_TOKEN" and fail_operator_after_apply:
            fail_operator_after_apply = False
            return 9
        return 0

    arguments = {
        "root": root,
        "repository_root": repository,
        "vercel_cli": cli,
        "vercel_cli_sha256": cli_digest,
        "vercel_cli_version": CLI_VERSION,
        "vercel_cwd": vercel_cwd,
        "environment": "preview",
        "project": PROJECT_ID,
        "scope": SCOPE_ID,
        "runner": runner,
        "metadata_reader": metadata_reader,
        "version_reader": lambda _executable, _cwd, _environment: CLI_VERSION,
        "unprotector": _unprotector,
        "platform_name": "nt",
    }
    with pytest.raises(
        IdentityProvisioningError,
        match="VERCEL_ENV_CONFIGURATION_FAILED_CONTROL_OPERATOR_TOKEN",
    ):
        configure_vercel_identity(**arguments)

    assert write_calls[0][:3] == (
        "POST",
        "CONTROL_OPERATOR_TOKEN",
        f"/v10/projects/{PROJECT_ID}/env",
    )
    assert preview["CONTROL_OPERATOR_TOKEN"]["id"] == "preview_created_operator"
    assert "CONTROL_IDENTITY_BUNDLE_DIGEST" not in preview
    assert production_ids == {
        name: f"production_record_{index}" for index, name in enumerate(names)
    }

    write_calls.clear()
    result = configure_vercel_identity(**arguments)

    assert result.configured_count == 8
    assert write_calls[0][:3] == (
        "PATCH",
        "CONTROL_OPERATOR_TOKEN",
        f"/v9/projects/{PROJECT_ID}/env/preview_created_operator",
    )
    assert "key" not in write_calls[0][3]
    assert (
        sum(
            method == "POST" and name == "CONTROL_OPERATOR_TOKEN"
            for method, name, _endpoint, _body in write_calls
        )
        == 0
    )
    assert write_calls[-1][1] == "CONTROL_IDENTITY_BUNDLE_DIGEST"
    assert set(preview) == set(names)
    assert production_ids == {
        name: f"production_record_{index}" for index, name in enumerate(names)
    }


def test_configure_vercel_rejects_target_link_digest_and_version_mismatch(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    common = {
        "root": root,
        "repository_root": repository,
        "vercel_cli": cli,
        "vercel_cli_sha256": cli_digest,
        "vercel_cli_version": CLI_VERSION,
        "vercel_cwd": vercel_cwd,
        "environment": "production",
        "project": PROJECT_ID,
        "scope": SCOPE_ID,
        "dry_run": True,
        "platform_name": "nt",
    }
    with pytest.raises(IdentityProvisioningError, match="IDENTITY_VERCEL_TARGET_MISMATCH"):
        configure_vercel_identity(**dict(common, environment="preview"))
    with pytest.raises(IdentityProvisioningError, match="VERCEL_CLI_DIGEST_MISMATCH"):
        configure_vercel_identity(**dict(common, vercel_cli_sha256="0" * 64))

    link_path = vercel_cwd / ".vercel" / "project.json"
    link_path.write_text(
        json.dumps({"orgId": SCOPE_ID, "projectId": "prj_deadbeef12345678"}),
        encoding="utf-8",
    )
    with pytest.raises(IdentityProvisioningError, match="VERCEL_PROJECT_LINK_MISMATCH"):
        configure_vercel_identity(**common)

    link_path.write_text(
        json.dumps({"orgId": SCOPE_ID, "projectId": PROJECT_ID}),
        encoding="utf-8",
    )
    decrypted = False

    def no_decrypt(_value: bytes) -> bytes:
        nonlocal decrypted
        decrypted = True
        raise AssertionError

    with pytest.raises(IdentityProvisioningError, match="VERCEL_CLI_VERSION_MISMATCH"):
        configure_vercel_identity(
            **dict(common, dry_run=False),
            version_reader=lambda _executable, _cwd, _environment: "58.0.0",
            unprotector=no_decrypt,
        )
    assert decrypted is False


def test_selected_identity_rejects_runner_private_public_mismatch(tmp_path: Path) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)
    selected = load_selected_identity(root=root, repository_root=repository)
    private_path = selected.bundle_path / "runner-private.key"
    private_text = private_path.read_text(encoding="ascii")
    replacement = ("A" if private_text[0] != "A" else "B") + private_text[1:]
    private_path.write_text(replacement, encoding="ascii")

    with pytest.raises(
        IdentityProvisioningError,
        match="RUNNER_SIGNING_IDENTITY_MISMATCH",
    ):
        load_selected_identity(root=root, repository_root=repository)


def test_protected_identity_json_rejects_duplicates_and_non_strings(tmp_path: Path) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)
    selected = load_selected_identity(root=root, repository_root=repository)
    protected = selected.secret_bundle_path.read_bytes()
    plaintext = _unprotector(protected)

    duplicate = plaintext.replace(
        b'"operator_token":',
        b'"operator_token":"duplicate","operator_token":',
        1,
    )
    with pytest.raises(IdentityProvisioningError, match="SECRET_BUNDLE_INVALID"):
        load_control_secrets(
            selected.secret_bundle_path,
            unprotector=lambda _value: duplicate,
        )

    payload = json.loads(plaintext)
    payload["operator_token"] = 42
    with pytest.raises(IdentityProvisioningError, match="SECRET_BUNDLE_INVALID"):
        load_control_secrets(
            selected.secret_bundle_path,
            unprotector=lambda _value: json.dumps(payload).encode("ascii"),
        )


def test_validate_selection_checks_dpapi_payload_and_both_signing_bindings(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)

    selected = validate_selected_identity(
        root=root,
        repository_root=repository,
        unprotector=_unprotector,
    )
    assert selected.vercel_environment == "production"
    assert selected.vercel_project_id == PROJECT_ID
    assert selected.vercel_scope_id == SCOPE_ID

    with pytest.raises(IdentityProvisioningError, match="SECRET_BUNDLE_INVALID"):
        validate_selected_identity(
            root=root,
            repository_root=repository,
            unprotector=lambda _value: b"{}",
        )


def test_configure_rejects_control_private_public_mismatch(tmp_path: Path) -> None:
    repository, root, _runtime_env, cli, vercel_cwd, cli_digest = _test_identity(tmp_path)
    selected = load_selected_identity(root=root, repository_root=repository)
    payload = json.loads(_unprotector(selected.secret_bundle_path.read_bytes()))
    private_text = payload["control_private_key"]
    payload["control_private_key"] = ("A" if private_text[0] != "A" else "B") + private_text[1:]

    with pytest.raises(
        IdentityProvisioningError,
        match="CONTROL_SIGNING_IDENTITY_MISMATCH",
    ):
        configure_vercel_identity(
            root=root,
            repository_root=repository,
            vercel_cli=cli,
            vercel_cli_sha256=cli_digest,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=vercel_cwd,
            environment="production",
            project=PROJECT_ID,
            scope=SCOPE_ID,
            runner=lambda *_arguments: 0,
            metadata_reader=_empty_vercel_metadata,
            version_reader=lambda _executable, _cwd, _environment: CLI_VERSION,
            unprotector=lambda _value: json.dumps(payload).encode("ascii"),
            platform_name="nt",
        )


def test_configure_vercel_refuses_non_windows_even_for_dry_run(tmp_path: Path) -> None:
    with pytest.raises(IdentityProvisioningError, match="WINDOWS_REQUIRED"):
        configure_vercel_identity(
            root=(tmp_path / "identity").resolve(),
            repository_root=(tmp_path / "repository").resolve(),
            vercel_cli=(tmp_path / "vercel.exe").resolve(),
            vercel_cli_sha256="0" * 64,
            vercel_cli_version=CLI_VERSION,
            vercel_cwd=(tmp_path / "repository" / "control_plane").resolve(),
            environment="preview",
            project=PROJECT_ID,
            scope=SCOPE_ID,
            dry_run=True,
            platform_name="posix",
        )


def test_windows_clipboard_uses_owned_non_null_window_for_set_clipboard_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state: dict[str, object] = {
        "opened_owner": None,
        "destroyed": False,
    }
    allocation = ctypes.create_string_buffer(256)
    allocation_address = ctypes.addressof(allocation)

    def create_window(*_arguments: object) -> int:
        return 4242

    def is_window(handle: object) -> int:
        return int(handle == 4242 and not state["destroyed"])

    def open_clipboard(handle: object) -> int:
        state["opened_owner"] = handle
        return int(handle == 4242)

    def set_clipboard_data(_format: object, handle: object) -> object:
        if state["opened_owner"] != 4242:
            return 0
        return handle

    user32 = SimpleNamespace(
        CreateWindowExW=_FakeCFunction(create_window),
        DestroyWindow=_FakeCFunction(lambda _handle: state.__setitem__("destroyed", True) or 1),
        IsWindow=_FakeCFunction(is_window),
        OpenClipboard=_FakeCFunction(open_clipboard),
        CloseClipboard=_FakeCFunction(lambda: 1),
        EmptyClipboard=_FakeCFunction(lambda: 1),
        IsClipboardFormatAvailable=_FakeCFunction(lambda _format: 0),
        GetClipboardData=_FakeCFunction(lambda _format: 0),
        SetClipboardData=_FakeCFunction(set_clipboard_data),
        GetClipboardSequenceNumber=_FakeCFunction(lambda: 17),
    )
    kernel32 = SimpleNamespace(
        GetModuleHandleW=_FakeCFunction(lambda _name: 31337),
        GlobalAlloc=_FakeCFunction(lambda _flags, _size: allocation_address),
        GlobalFree=_FakeCFunction(lambda _handle: 0),
        GlobalLock=_FakeCFunction(lambda handle: handle),
        GlobalSize=_FakeCFunction(lambda _handle: len(allocation)),
        GlobalUnlock=_FakeCFunction(lambda _handle: 1),
    )
    monkeypatch.setattr(identity_module.os, "name", "nt")
    monkeypatch.setattr(
        identity_module.ctypes,
        "windll",
        SimpleNamespace(user32=user32, kernel32=kernel32),
        raising=False,
    )

    clipboard = WindowsClipboard()
    lease = clipboard.set_text("bounded-test-token")

    assert lease.sequence_number == 17
    assert state["opened_owner"] == 4242
    assert state["opened_owner"] is not None
    clipboard._destroy_owner_window()
    assert state["destroyed"] is True


@pytest.mark.parametrize("sequence_values", [(0,), (17, 0)])
def test_windows_clipboard_rejects_zero_on_either_cleanup_sequence_read(
    sequence_values: tuple[int, ...],
) -> None:
    encoded = "bounded-test-token\0".encode("utf-16-le")
    allocation = ctypes.create_string_buffer(encoded)
    allocation_address = ctypes.addressof(allocation)
    sequences = iter(sequence_values)
    controller = object.__new__(WindowsClipboard)
    controller._owner_window = 4242
    controller._user32 = SimpleNamespace(
        IsWindow=lambda _handle: 1,
        OpenClipboard=lambda _handle: 1,
        CloseClipboard=lambda: 1,
        DestroyWindow=lambda _handle: 1,
        GetClipboardSequenceNumber=lambda: next(sequences),
        IsClipboardFormatAvailable=lambda _format: 1,
        GetClipboardData=lambda _format: allocation_address,
        EmptyClipboard=lambda: 1,
    )
    controller._kernel32 = SimpleNamespace(
        GlobalSize=lambda _handle: len(encoded),
        GlobalLock=lambda handle: handle,
        GlobalUnlock=lambda _handle: 1,
    )

    with pytest.raises(
        IdentityProvisioningError,
        match="CLIPBOARD_SEQUENCE_UNAVAILABLE",
    ):
        controller.clear_if_unchanged(
            "bounded-test-token",
            ClipboardLease(sequence_number=17),
        )


def test_copy_operator_token_clears_only_unchanged_clipboard(tmp_path: Path) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)
    clipboard = _FakeClipboard()
    warnings: list[str] = []
    slept: list[float] = []

    cleared = copy_operator_token(
        root=root,
        repository_root=repository,
        ttl_seconds=15,
        clipboard=clipboard,
        sleeper=slept.append,
        warning_writer=warnings.append,
        unprotector=_unprotector,
        platform_name="nt",
    )

    assert cleared is True
    assert clipboard.value is None
    assert clipboard.clear_calls == 1
    assert slept == [15.0]
    assert len(warnings) == 2
    assert all(
        "operator token" not in warning.casefold() or "warning" in warning.casefold()
        for warning in warnings
    )
    protected = load_control_secrets(
        load_selected_identity(root=root, repository_root=repository).secret_bundle_path,
        unprotector=_unprotector,
    )
    assert all(str(protected["operator_token"]) not in warning for warning in warnings)


def test_copy_operator_token_leaves_replacement_clipboard_untouched(tmp_path: Path) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)
    clipboard = _FakeClipboard()

    def replace_clipboard(_seconds: float) -> None:
        clipboard.value = "replacement-value"
        clipboard.sequence += 1

    cleared = copy_operator_token(
        root=root,
        repository_root=repository,
        ttl_seconds=30,
        clipboard=clipboard,
        sleeper=replace_clipboard,
        warning_writer=lambda _message: None,
        unprotector=_unprotector,
        platform_name="nt",
    )

    assert cleared is False
    assert clipboard.value == "replacement-value"
    assert clipboard.clear_calls == 1


def test_copy_operator_token_does_not_clear_same_text_after_sequence_change(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)
    clipboard = _FakeClipboard()

    def rewrite_same_text(_seconds: float) -> None:
        clipboard.sequence += 1

    cleared = copy_operator_token(
        root=root,
        repository_root=repository,
        ttl_seconds=15,
        clipboard=clipboard,
        sleeper=rewrite_same_text,
        warning_writer=lambda _message: None,
        unprotector=_unprotector,
        platform_name="nt",
    )

    assert cleared is False
    assert clipboard.value is not None


def test_copy_operator_token_retries_transient_clipboard_lock(tmp_path: Path) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)

    class TransientClipboard(_FakeClipboard):
        failures = 2

        def clear_if_unchanged(self, expected: str, lease: ClipboardLease) -> bool:
            if self.failures:
                self.failures -= 1
                raise IdentityProvisioningError("CLIPBOARD_UNAVAILABLE")
            return super().clear_if_unchanged(expected, lease)

    clipboard = TransientClipboard()
    retries: list[float] = []
    assert copy_operator_token(
        root=root,
        repository_root=repository,
        ttl_seconds=15,
        clipboard=clipboard,
        sleeper=lambda _seconds: None,
        retry_sleeper=retries.append,
        warning_writer=lambda _message: None,
        unprotector=_unprotector,
        platform_name="nt",
    )
    assert retries == [0.1, 0.1]
    assert clipboard.value is None


def test_copy_operator_token_cleans_up_when_ttl_sleep_raises(tmp_path: Path) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)
    clipboard = _FakeClipboard()

    def interrupted(_seconds: float) -> None:
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        copy_operator_token(
            root=root,
            repository_root=repository,
            ttl_seconds=15,
            clipboard=clipboard,
            sleeper=interrupted,
            warning_writer=lambda _message: None,
            unprotector=_unprotector,
            platform_name="nt",
        )
    assert clipboard.value is None


@pytest.mark.parametrize("primary_failure", ["ttl", "warning"])
def test_copy_operator_token_prioritizes_cleanup_failure_after_primary_exception(
    tmp_path: Path,
    primary_failure: str,
) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)

    class PermanentlyLockedClipboard(_FakeClipboard):
        def clear_if_unchanged(self, expected: str, lease: ClipboardLease) -> bool:
            self.clear_calls += 1
            raise IdentityProvisioningError("CLIPBOARD_UNAVAILABLE")

    clipboard = PermanentlyLockedClipboard()
    warnings: list[str] = []

    def warning_writer(message: str) -> None:
        warnings.append(message)
        if primary_failure == "warning" and len(warnings) == 1:
            raise RuntimeError("warning failed")

    def sleeper(_seconds: float) -> None:
        if primary_failure == "ttl":
            raise RuntimeError("ttl interrupted")

    with pytest.raises(
        IdentityProvisioningError,
        match="CLIPBOARD_CLEANUP_FAILED",
    ):
        copy_operator_token(
            root=root,
            repository_root=repository,
            ttl_seconds=15,
            clipboard=clipboard,
            sleeper=sleeper,
            retry_sleeper=lambda _seconds: None,
            warning_writer=warning_writer,
            unprotector=_unprotector,
            platform_name="nt",
        )

    assert clipboard.clear_calls == 5
    assert clipboard.value is not None
    assert any("clear the operator token manually now" in warning for warning in warnings)


def test_copy_operator_token_cleans_up_when_retry_delay_is_interrupted(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)

    class OnceLockedClipboard(_FakeClipboard):
        locked = True

        def clear_if_unchanged(self, expected: str, lease: ClipboardLease) -> bool:
            if self.locked:
                self.locked = False
                raise IdentityProvisioningError("CLIPBOARD_UNAVAILABLE")
            return super().clear_if_unchanged(expected, lease)

    clipboard = OnceLockedClipboard()
    with pytest.raises(RuntimeError, match="retry interrupted"):
        copy_operator_token(
            root=root,
            repository_root=repository,
            ttl_seconds=15,
            clipboard=clipboard,
            sleeper=lambda _seconds: None,
            retry_sleeper=lambda _seconds: (_ for _ in ()).throw(RuntimeError("retry interrupted")),
            warning_writer=lambda _message: None,
            unprotector=_unprotector,
            platform_name="nt",
        )
    assert clipboard.value is None


def test_copy_operator_token_fails_loudly_when_cleanup_remains_locked(
    tmp_path: Path,
) -> None:
    repository, root, _runtime_env, _cli, _cwd, _digest = _test_identity(tmp_path)

    class LockedClipboard(_FakeClipboard):
        def clear_if_unchanged(self, expected: str, lease: ClipboardLease) -> bool:
            raise IdentityProvisioningError("CLIPBOARD_UNAVAILABLE")

    clipboard = LockedClipboard()
    warnings: list[str] = []
    with pytest.raises(IdentityProvisioningError, match="CLIPBOARD_CLEANUP_FAILED"):
        copy_operator_token(
            root=root,
            repository_root=repository,
            ttl_seconds=15,
            clipboard=clipboard,
            sleeper=lambda _seconds: None,
            retry_sleeper=lambda _seconds: None,
            warning_writer=warnings.append,
            unprotector=_unprotector,
            platform_name="nt",
        )
    assert clipboard.value is not None
    assert any("manually" in warning for warning in warnings)


@pytest.mark.parametrize("ttl_seconds", [0, 14, 301, 1_000])
def test_copy_operator_token_ttl_is_bounded(
    tmp_path: Path,
    ttl_seconds: int,
) -> None:
    with pytest.raises(IdentityProvisioningError, match="CLIPBOARD_TTL_INVALID"):
        copy_operator_token(
            root=(tmp_path / "identity").resolve(),
            repository_root=(tmp_path / "repository").resolve(),
            ttl_seconds=ttl_seconds,
            clipboard=_FakeClipboard(),
            sleeper=lambda _seconds: None,
            warning_writer=lambda _message: None,
            unprotector=_unprotector,
            platform_name="nt",
        )
