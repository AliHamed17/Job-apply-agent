"""Fail-closed deployment boundary tests for the public control plane."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL_PLANE_ROOT = REPO_ROOT / "control_plane"

ALLOWED_CONTROL_PLANE_ROOT_ENTRIES = frozenset(
    {
        ".gitignore",
        ".vercelignore",
        "MIGRATIONS.md",
        "README.md",
        "alembic.ini",
        "api",
        "job_control_plane",
        "migrations",
        "pyproject.toml",
        "requirements.txt",
        "tests",
        "vercel.json",
    }
)

ROOT_FALLBACK_BUNDLE_ENTRIES = frozenset(
    {
        "api",
        "job_control_plane",
        "requirements.txt",
    }
)

PRIVATE_ROOT_IMPORTS = frozenset(
    {
        "api",
        "bridge",
        "core",
        "db",
        "discovery",
        "ingestion",
        "jobs",
        "llm",
        "match",
        "notifications",
        "profile",
        "scripts",
        "submitters",
        "worker",
    }
)

FORBIDDEN_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".linkedin_profile",
        ".portal_profiles",
        ".vercel",
        "__pycache__",
        "browser-data",
        "browser-state",
        "cvs",
        "data",
        "device-state",
        "profile-data",
        "profiles",
        "runtime-data",
        "session-data",
        "sessions",
        "uploads",
    }
)

FORBIDDEN_SUFFIXES = frozenset(
    {
        ".db",
        ".doc",
        ".docx",
        ".key",
        ".log",
        ".p12",
        ".pdf",
        ".pem",
        ".pfx",
        ".rtf",
        ".sqlite",
        ".sqlite3",
    }
)

GENERATED_DIRECTORY_NAMES = frozenset(
    {
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
    }
)


def _active_ignore_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _python_import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def test_root_vercel_routes_only_to_isolated_control_plane() -> None:
    config = json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))

    assert config == {
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "builds": [
            {
                "src": "control_plane/api/index.py",
                "use": "@vercel/python",
            }
        ],
        "routes": [
            {
                "src": "/(.*)",
                "dest": "control_plane/api/index.py",
            }
        ],
    }
    assert (REPO_ROOT / config["builds"][0]["src"]).is_file()


def test_isolated_vercel_project_cannot_escape_its_own_root() -> None:
    config = json.loads((CONTROL_PLANE_ROOT / "vercel.json").read_text(encoding="utf-8"))

    assert config == {
        "$schema": "https://openapi.vercel.sh/vercel.json",
        "version": 2,
        "builds": [
            {
                "src": "api/index.py",
                "use": "@vercel/python",
            }
        ],
        "routes": [
            {
                "src": "/(.*)",
                "dest": "api/index.py",
            }
        ],
    }
    assert ".." not in json.dumps(config)
    assert (CONTROL_PLANE_ROOT / config["builds"][0]["src"]).is_file()


def test_root_vercel_upload_is_an_explicit_allowlist() -> None:
    lines = _active_ignore_lines(REPO_ROOT / ".vercelignore")
    negated = {line for line in lines if line.startswith("!")}

    assert lines[0] == "/*"
    assert negated == {
        "!control_plane",
        "!control_plane/api",
        "!control_plane/api/**",
        "!control_plane/job_control_plane",
        "!control_plane/job_control_plane/**",
        "!control_plane/requirements.txt",
        "!vercel.json",
    }
    assert "control_plane/*" in lines
    assert all(
        not line.startswith(("!api", "!core", "!db", "!profile", "!worker")) for line in lines
    )


def test_control_plane_tree_contains_only_public_bundle_entries() -> None:
    assert CONTROL_PLANE_ROOT.is_dir()
    root_entries = {
        path.name
        for path in CONTROL_PLANE_ROOT.iterdir()
        if path.name.casefold() not in GENERATED_DIRECTORY_NAMES
    }
    assert root_entries <= ALLOWED_CONTROL_PLANE_ROOT_ENTRIES

    for path in CONTROL_PLANE_ROOT.rglob("*"):
        relative = path.relative_to(CONTROL_PLANE_ROOT)
        lowered_parts = tuple(part.casefold() for part in relative.parts)
        lowered_name = path.name.casefold()

        assert not path.is_symlink(), f"symlinks are forbidden in the public bundle: {relative}"
        if GENERATED_DIRECTORY_NAMES.intersection(lowered_parts):
            continue
        if relative.parts[0] not in ROOT_FALLBACK_BUNDLE_ENTRIES:
            continue
        assert not FORBIDDEN_DIRECTORY_NAMES.intersection(lowered_parts), relative
        assert path.suffix.casefold() not in FORBIDDEN_SUFFIXES, relative
        assert not lowered_name.startswith((".env", "user_profile", "cv_routing", "resume"))
        assert "secret" not in lowered_name
        assert "private" not in lowered_name


def test_control_plane_production_code_cannot_import_private_root_modules() -> None:
    for path in CONTROL_PLANE_ROOT.rglob("*.py"):
        relative = path.relative_to(CONTROL_PLANE_ROOT)
        if relative.parts[0] == "tests":
            continue
        imported = _python_import_roots(path)
        assert not imported.intersection(PRIVATE_ROOT_IMPORTS), (
            f"{relative} imports private repository modules: "
            f"{sorted(imported.intersection(PRIVATE_ROOT_IMPORTS))}"
        )


def test_isolated_project_ignore_file_denies_sensitive_runtime_artifacts() -> None:
    lines = _active_ignore_lines(CONTROL_PLANE_ROOT / ".vercelignore")
    joined = "\n".join(lines).casefold()

    required_markers = (
        ".env",
        ".vercel",
        "*.db",
        "*.sqlite",
        "*.sqlite3",
        "*.pdf",
        "*.doc",
        "*.docx",
        "*.key",
        "*.pem",
        "cvs",
        "profile",
        "session",
        "browser",
    )
    for marker in required_markers:
        assert marker in joined, f"missing isolated-bundle deny rule for {marker}"


def test_repository_ignore_file_protects_local_control_plane_state() -> None:
    joined = "\n".join(_active_ignore_lines(REPO_ROOT / ".gitignore")).casefold()

    required_markers = (
        "control_plane/.env",
        "control_plane/.vercel",
        "control_plane/**/*.db",
        "control_plane/**/*.sqlite",
        "control_plane/**/*.sqlite3",
        "control_plane/**/*.pem",
        "control_plane/**/*.p12",
        "control_plane/**/*.pfx",
        "control_plane/**/browser",
        "control_plane/**/device-state",
        "control_plane/**/session",
        "control_plane/**/*token*",
    )
    for marker in required_markers:
        assert marker in joined, f"missing repository ignore rule for {marker}"


def test_legacy_root_entrypoint_exposes_only_public_liveness() -> None:
    path = REPO_ROOT / "api" / "vercel.py"
    imported = _python_import_roots(path)
    assert imported == {"fastapi"}

    spec = importlib.util.spec_from_file_location("_deployment_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert {route.path for route in module.app.routes} == {"/health/live"}

    client = TestClient(module.app)
    assert client.get("/health/live").json() == {
        "status": "ok",
        "service": "deployment-guard",
    }
    assert client.get("/").status_code == 404
    assert client.get("/api/applications").status_code == 404
    assert client.post("/api/applications/1/submit").status_code == 404


def test_all_compose_host_ports_are_loopback_bound() -> None:
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    published_ports = [
        port for service in compose["services"].values() for port in service.get("ports", [])
    ]

    assert published_ports
    assert all(str(port).startswith("127.0.0.1:") for port in published_ports)
