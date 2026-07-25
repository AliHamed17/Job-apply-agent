from __future__ import annotations

import pytest

from core.config import Settings
from scripts.run_safe_automation import validate_safe_mode


def test_safe_runner_requires_both_guards():
    validate_safe_mode(Settings(_env_file=None, dry_run=True, draft_only=True))

    with pytest.raises(RuntimeError, match="DRY_RUN"):
        validate_safe_mode(Settings(_env_file=None, dry_run=False, draft_only=True))

    with pytest.raises(RuntimeError, match="DRAFT_ONLY"):
        validate_safe_mode(Settings(_env_file=None, dry_run=True, draft_only=False))
