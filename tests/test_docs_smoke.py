from pathlib import Path

import yaml


def test_compose_has_beat_service():
    compose = yaml.safe_load(Path("docker-compose.yml").read_text(encoding="utf-8"))
    assert "celery-beat" in compose["services"]


def test_readme_documents_current_safety_boundary():
    txt = Path("README.md").read_text(encoding="utf-8").lower()
    assert "not a universal or unattended auto-applier" in txt
    assert "fixture_qualified" in txt
    assert "exactly 87 sanitized fixtures" in txt
    assert "workday 9, greenhouse 22, lever 28, ashby 13" in txt
    assert "smartrecruiters 15" in txt
    assert "real-url dry runs completed: 0" in txt
    assert "live canaries completed: 0" in txt
    assert "qualified form fingerprints/scopes: 0" in txt
    assert "enabled final executors: 0" in txt
    assert "qwen2.5:7b" in txt
    assert "explicit operator action" in txt
    assert "`get /health/live`, `get /health/ready`, and" in txt
    assert "`get /metrics` unauthenticated" in txt
    assert "bind them to loopback or an internal network" in txt
    assert "detailed application and operational api" in txt
    assert "routes require bearer authentication" in txt
    assert "full_auto=" not in txt


def test_managed_runtime_docs_use_authoritative_external_data_roots():
    backup = Path("docs/control-plane-backup-restore.md").read_text(encoding="utf-8")
    retention = Path("docs/private-data-retention.md").read_text(encoding="utf-8")
    bootstrap = Path("docs/control-plane-bootstrap.md").read_text(encoding="utf-8")

    profile_root = r"$env:LOCALAPPDATA\JobApplyAgent\profile-data"
    browser_root = r"$env:LOCALAPPDATA\JobApplyAgent\browser-state"

    for document in (backup, retention, bootstrap):
        assert profile_root in document
        assert browser_root in document

    assert "JOB_AGENT_PROFILE_DATA_DIR" in backup
    assert "JOB_AGENT_BROWSER_STATE_DIR" in backup
    assert "legacy repo-relative" in backup
    assert "legacy direct-run defaults only" in retention
    assert "does not copy personal files" in bootstrap
    assert "browser sessions from the checkout" in bootstrap


def test_control_plane_docs_use_staged_production_for_exact_artifact_promotion():
    bootstrap = Path("docs/control-plane-bootstrap.md").read_text(encoding="utf-8")
    control_readme = Path("control_plane/README.md").read_text(encoding="utf-8")

    for document in (bootstrap, control_readme):
        assert "vercel deploy --prod --skip-domain" in document
        assert "vercel promote <" in document
        assert "staged Production" in document

    assert "Project Root exactly to this `control_plane` directory" in control_readme
    assert "Vercel rebuilds a promoted Preview" in bootstrap


def test_control_plane_docs_keep_identity_secrets_off_argv_and_logs():
    bootstrap = Path("docs/control-plane-bootstrap.md").read_text(encoding="utf-8")
    recovery = Path("docs/recovery-runbooks.md").read_text(encoding="utf-8")
    normalized_bootstrap = " ".join(bootstrap.split())

    assert "configure-vercel" in bootstrap
    assert "--dry-run" in bootstrap
    assert "does not decrypt the DPAPI bundle or invoke Vercel" in normalized_bootstrap
    assert "only through the pinned" in normalized_bootstrap
    assert "standard input" in normalized_bootstrap
    assert "never enter argv" in normalized_bootstrap
    assert "schema-v2 identity" in normalized_bootstrap
    assert "exact linked cwd/project/scope" in normalized_bootstrap
    assert "npm `.cmd` shim is intentionally rejected" in normalized_bootstrap
    assert "--vercel-node-sha256" in bootstrap
    assert "--vercel-js-entrypoint-sha256" in bootstrap
    assert "patches that exact record ID through `vercel api`" in normalized_bootstrap
    assert "never uses the key-only `env add --force` upsert" in normalized_bootstrap
    assert "do not use `Get-Command vercel.exe`" in normalized_bootstrap
    assert "Join-Path $vercelNativePackageRoot 'bin\\vercel.exe'" in bootstrap
    assert "outside OneDrive or any other synced/reparse directory" in normalized_bootstrap
    assert "C:\\JobApplyAgentDeploy" in bootstrap
    assert "validate-selection" in recovery
    assert "`CONTROL_IDENTITY_BUNDLE_DIGEST`, written last" in normalized_bootstrap
    assert "copy-operator-token" in bootstrap
    assert "native sequence number and text are unchanged" in normalized_bootstrap
    assert "exits nonzero" in normalized_bootstrap
    normalized_recovery = " ".join(recovery.casefold().split())
    assert (
        "configure the database, origin, and application environment separately"
        in normalized_recovery
    )
