"""Initialize or inspect the private local qualified-autopilot signing key."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

from core.automation_policy_keys import (
    AutomationPolicyKeyError,
    configured_automation_policy_key_path,
    generate_automation_policy_signing_key,
    load_automation_policy_signing_identity,
)


def _harden_windows_acl(path: Path) -> None:
    if os.name != "nt":
        return
    username = os.environ.get("USERNAME", "").strip()
    if not username:
        raise AutomationPolicyKeyError("AUTOMATION_POLICY_KEY_ACL_UNAVAILABLE")
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{username}:(R,W)",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise AutomationPolicyKeyError("AUTOMATION_POLICY_KEY_ACL_UNAVAILABLE")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python scripts/automation_policy_key.py")
    parser.add_argument("command", choices=("init", "status"))
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    path = args.path or configured_automation_policy_key_path()
    try:
        if args.command == "init":
            key_id = generate_automation_policy_signing_key(path)
            try:
                _harden_windows_acl(path)
            except AutomationPolicyKeyError:
                path.unlink(missing_ok=True)
                raise
        else:
            key_id = load_automation_policy_signing_identity(path).key_id
    except AutomationPolicyKeyError as exc:
        print(exc.reason_code)
        return 1
    print(f"key_id={key_id}")
    print(f"path={path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
