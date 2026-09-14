"""Isolate runtime files created by tests from the production tree."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_cron_alert_state_file(tmp_path, monkeypatch):
    """Point the 24h gate state file at a per-test tmp path.

    civitai-bot.py is imported under several module names; an env override
    is resolved at call time so every copy honours this without patching
    each module object. Production (no env) still writes under SCRIPT_DIR.
    """
    monkeypatch.setenv(
        "CIVITAI_CRON_ALERT_STATE", str(tmp_path / "cron_alert_state.json")
    )
