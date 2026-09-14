"""24h cron-alert gate: window, one-shot fail, recover, persisted restart."""

from __future__ import annotations

import importlib.util
import json
import sys
from http import HTTPStatus
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "civitai_bot_cron_alert_gate",
    str(Path(__file__).parent.parent / "civitai-bot.py"),
)
civitai_bot = importlib.util.module_from_spec(_spec)
sys.modules["civitai_bot_cron_alert_gate"] = civitai_bot
_spec.loader.exec_module(civitai_bot)

WINDOW = civitai_bot.CRON_ALERT_WINDOW_S
T0 = 1_700_000_000.0  # arbitrary epoch; tests pass `now=` explicitly


class _AlertBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.fixture
def gate_env(monkeypatch, tmp_path):
    """Fresh in-memory + on-disk gate state, isolated from production."""
    state_path = tmp_path / "cron_alert_state.json"
    monkeypatch.setenv("CIVITAI_CRON_ALERT_STATE", str(state_path))
    monkeypatch.setattr(civitai_bot, "_cron_alert_state", {})
    monkeypatch.setattr(civitai_bot, "_cron_alert_disk_loaded", False)
    bot = _AlertBot()
    monkeypatch.setattr(civitai_bot, "_ADMIN_CHAT_IDS", [12345])
    monkeypatch.setattr(civitai_bot, "_alert_bot", bot)
    return {"path": state_path, "bot": bot}


def _gate(job="scan", *, failing, now):
    return civitai_bot._cron_alert_gate(job, failing=failing, now=now)


def test_success_inside_window_does_not_alert(gate_env):
    """A failure that still has a success inside the 24h window stays silent."""
    assert _gate(failing=False, now=T0) is None
    assert _gate(failing=True, now=T0 + 60) is None
    assert _gate(failing=True, now=T0 + WINDOW - 1) is None
    assert gate_env["path"].is_file()
    saved = json.loads(gate_env["path"].read_text(encoding="utf-8"))
    assert saved["scan"]["last_success_ts"] == T0
    assert saved["scan"]["alerted"] is False


def test_full_24h_of_failures_alerts_once_and_does_not_repeat(gate_env):
    """First failure at/after 24h pages once; later failures the same day do not."""
    assert _gate(failing=False, now=T0) is None
    assert _gate(failing=True, now=T0 + WINDOW - 1) is None
    assert _gate(failing=True, now=T0 + WINDOW) == "fail"
    assert _gate(failing=True, now=T0 + WINDOW + 600) is None
    assert _gate(failing=True, now=T0 + WINDOW + 3600) is None
    # next UTC day -> one digest, then silent again that day
    next_day = T0 + WINDOW + 24 * 3600
    assert _gate(failing=True, now=next_day) == "digest"
    assert _gate(failing=True, now=next_day + 60) is None


def test_success_after_alert_sends_recover(gate_env):
    """Recovery notice fires only if we actually alerted, then the clock resets."""
    assert _gate(failing=True, now=T0) is None  # bootstrap, no history
    assert _gate(failing=False, now=T0 + 10) is None  # success, never alerted
    assert _gate(failing=True, now=T0 + 10 + WINDOW) == "fail"
    assert _gate(failing=False, now=T0 + 10 + WINDOW + 5) == "recover"
    assert _gate(failing=False, now=T0 + 10 + WINDOW + 6) is None
    # new failure episode needs another full 24h
    assert _gate(failing=True, now=T0 + 10 + WINDOW + 7) is None


def test_state_file_survives_process_restart(gate_env):
    """Clearing in-memory state reloads last_success_ts / alerted from disk."""
    assert _gate(failing=False, now=T0) is None
    assert _gate(failing=True, now=T0 + WINDOW) == "fail"
    saved = json.loads(gate_env["path"].read_text(encoding="utf-8"))
    assert saved["scan"]["alerted"] is True
    assert saved["scan"]["last_success_ts"] == T0

    civitai_bot._cron_alert_state.clear()
    civitai_bot._cron_alert_disk_loaded = False

    # same UTC day after restart: must not re-page
    assert _gate(failing=True, now=T0 + WINDOW + 30) is None
    # success after restart still recovers
    civitai_bot._cron_alert_state.clear()
    civitai_bot._cron_alert_disk_loaded = False
    assert _gate(failing=False, now=T0 + WINDOW + 40) == "recover"


def test_bootstrap_without_history_does_not_false_alarm(gate_env):
    """A first-ever failure (no file, empty memory) starts the clock, no alert."""
    assert not gate_env["path"].exists()
    assert _gate(failing=True, now=T0) is None
    saved = json.loads(gate_env["path"].read_text(encoding="utf-8"))
    assert saved["scan"]["last_success_ts"] == T0
    assert saved["scan"]["alerted"] is False


def test_scan_and_reconcile_jobs_are_independent(gate_env):
    _gate("scan", failing=False, now=T0)
    _gate("reconciliation", failing=False, now=T0)
    assert _gate("scan", failing=True, now=T0 + WINDOW) == "fail"
    # recon still inside its own success window
    assert _gate("reconciliation", failing=True, now=T0 + 60) is None
    assert _gate("reconciliation", failing=True, now=T0 + WINDOW) == "fail"


def test_humanize_http_timeout_connection_and_strips_bare_exit():
    h = civitai_bot._humanize_scan_error
    assert h("Page query failed (track=SFW): 503 Server Error: Service Unavailable") == (
        f"Civitai API 503 {HTTPStatus.SERVICE_UNAVAILABLE.phrase}"
    )
    assert h("502 Server Error: Bad Gateway for url: https://civitai.com/api/v1/x") == (
        f"Civitai API 502 {HTTPStatus.BAD_GATEWAY.phrase}"
    )
    assert h("429 Client Error: Too Many Requests") == (
        f"Civitai API 429 {HTTPStatus.TOO_MANY_REQUESTS.phrase}"
    )
    assert h("HTTPSConnectionPool: Read timed out. (read timeout=30)") == "连接超时"
    assert h("Max retries exceeded (Caused by ConnectionError)") == "连接错误"
    assert h("monitor.py 非零退出（exit 1），本轮扫描可能未完成") == ""
    assert h("") == ""


def test_last_scan_error_line_falls_back_to_informative_tail(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "2026-09-14 00:00:00 +0000 [INFO] civitai-bot: Scheduled scan starting...\n"
        "2026-09-14 00:00:01 +0000 [INFO] civitai-bot: Scheduled scan failed (exit 1)\n"
        "2026-09-14 00:00:02 +0000 [WARNING] civitai-monitor: "
        "HTTPSConnectionPool(host='civitai.com', port=443): Read timed out.\n",
        encoding="utf-8",
    )
    reason = civitai_bot._last_scan_error_line(str(log))
    assert "timed out" in reason.lower()
    assert "exit 1" not in reason


@pytest.mark.asyncio
async def test_report_title_names_http_error(gate_env, monkeypatch):
    """Fail title is the required shape, not a bare exit code."""
    monkeypatch.setattr(
        civitai_bot,
        "_last_scan_error_line",
        lambda _p=None: (
            "Page query failed (track=SFW): 503 Server Error: "
            "Service Unavailable for url: https://civitai.com/api/v1/images"
        ),
    )
    civitai_bot._cron_alert_disk_loaded = True
    civitai_bot._cron_alert_state["scan"] = {
        "last_success_ts": T0,
        "alerted": False,
        "last_alert_date": "",
    }
    await civitai_bot._report_cron_outcome(
        "scan", failing=True, detail="monitor.py 非零退出（exit 1）"
    )
    assert len(gate_env["bot"].sent) == 1
    text = gate_env["bot"].sent[0][1]
    assert text.startswith(
        f"❌ 定时扫描已连续 24 小时失败：Civitai API 503 {HTTPStatus.SERVICE_UNAVAILABLE.phrase}"
    )
    assert "exit 1" in text  # body may mention it; title already named the error


@pytest.mark.asyncio
async def test_recover_message_text(gate_env):
    _gate("scan", failing=False, now=T0)
    civitai_bot._cron_alert_state["scan"]["alerted"] = True
    await civitai_bot._report_cron_outcome("scan", failing=False)
    assert gate_env["bot"].sent == [(12345, "✅ 定时扫描已恢复正常")]
