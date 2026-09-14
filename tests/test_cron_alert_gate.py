"""24h cron-alert gate: window, one-shot fail, recover, persisted restart."""

from __future__ import annotations

import importlib.util
import json
import logging
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
    monkeypatch.setattr(civitai_bot, "_cron_alert_persist_had_failure", False)
    monkeypatch.setattr(civitai_bot, "_cron_alert_persist_warned_at", None)
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
    # Bare numeric tokens must not be titled as HTTP errors.
    assert "Civitai API" not in h("processed 503 items this round")
    assert "Civitai API" not in h("waited 500 ms for lock")
    assert h("503 Service Unavailable") == (
        f"Civitai API 503 {HTTPStatus.SERVICE_UNAVAILABLE.phrase}"
    )
    assert h("504 Gateway Timeout") == (
        f"Civitai API 504 {HTTPStatus.GATEWAY_TIMEOUT.phrase}"
    )
    # Real RateLimitError text from civitai_client.safe_get (no Server/Client Error).
    assert h("429 Rate Limited, retry after 30s") == (
        f"Civitai API 429 {HTTPStatus.TOO_MANY_REQUESTS.phrase}"
    )
    # nginx hyphenated gateway timeout.
    assert h("504 Gateway Time-out") == (
        f"Civitai API 504 {HTTPStatus.GATEWAY_TIMEOUT.phrase}"
    )
    # Explicit forms without a phrase next to the code.
    assert h("HTTP 502 from upstream") == (
        f"Civitai API 502 {HTTPStatus.BAD_GATEWAY.phrase}"
    )
    assert h("upstream status=503") == (
        f"Civitai API 503 {HTTPStatus.SERVICE_UNAVAILABLE.phrase}"
    )
    assert h("503, url='https://civitai.com/api/v1/images'") == (
        f"Civitai API 503 {HTTPStatus.SERVICE_UNAVAILABLE.phrase}"
    )
    assert h("ClientResponseError: 503, message='', url='https://civitai.com/api/v1/images'") == (
        f"Civitai API 503 {HTTPStatus.SERVICE_UNAVAILABLE.phrase}"
    )
    # Soft Timeout/Unavailable must not unlock a bare status number.
    assert "Civitai API" not in h("timed out after 500 ms")
    assert h("timed out after 500 ms") == "连接超时"
    assert "Civitai API" not in h("TimeoutError while saving 503 items")


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


def test_persist_failure_logs_warning_and_gate_still_alerts(gate_env, monkeypatch, caplog):
    """Write failure is a warning, never raised, and does not collapse the gate."""

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(civitai_bot, "_atomic_write", _boom)
    with caplog.at_level(logging.WARNING, logger="civitai-bot"):
        civitai_bot._persist_cron_alert_state()
        assert _gate(failing=True, now=T0) is None
        assert _gate(failing=True, now=T0 + WINDOW) == "fail"
    assert any(
        r.levelno == logging.WARNING
        and "Failed to persist cron alert state" in r.getMessage()
        for r in caplog.records
    )
    # in-memory fail still happened; same-day repeat stays silent
    assert _gate(failing=True, now=T0 + WINDOW + 60) is None


def test_corrupt_state_json_bootstraps_without_false_alarm(gate_env):
    """A truncated / invalid JSON file is read as {} (bootstrap, no page)."""
    path = gate_env["path"]
    path.write_text("{not-json", encoding="utf-8")
    assert _gate(failing=True, now=T0) is None
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["scan"]["last_success_ts"] == T0
    assert saved["scan"]["alerted"] is False


def test_unwritable_state_path_does_not_raise_and_fail_still_reachable(
    gate_env, monkeypatch, tmp_path
):
    """When the state path cannot be written, the 24h gate still reaches fail."""
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("blocked", encoding="utf-8")
    monkeypatch.setenv(
        "CIVITAI_CRON_ALERT_STATE", str(blocker / "cron_alert_state.json")
    )
    civitai_bot._cron_alert_state.clear()
    civitai_bot._cron_alert_disk_loaded = False
    assert _gate(failing=True, now=T0) is None
    assert _gate(failing=True, now=T0 + WINDOW) == "fail"
    assert _gate(failing=True, now=T0 + WINDOW + 60) is None


@pytest.mark.asyncio
async def test_reconciliation_title_does_not_borrow_scan_error(gate_env, monkeypatch):
    """Recon titles use own detail / generic fallback, never a scan log line."""
    monkeypatch.setattr(
        civitai_bot,
        "_last_scan_error_line",
        lambda _p=None: (
            "Page query failed (track=SFW): 503 Server Error: "
            "Service Unavailable for url: https://civitai.com/api/v1/images"
        ),
    )
    monkeypatch.setattr(civitai_bot, "_last_recon_error_line", lambda _p=None: "")
    civitai_bot._cron_alert_disk_loaded = True
    civitai_bot._cron_alert_state["reconciliation"] = {
        "last_success_ts": T0,
        "alerted": False,
        "last_alert_date": "",
    }
    await civitai_bot._report_cron_outcome(
        "reconciliation",
        failing=True,
        detail="reconcile 非零退出（exit 1），今日第 1/3 次重试",
    )
    assert len(gate_env["bot"].sent) == 1
    text = gate_env["bot"].sent[0][1]
    first_line = text.split("\n", 1)[0]
    assert "Civitai API 503" not in text
    assert "Page query failed" not in text
    assert "Service Unavailable" not in first_line
    assert first_line == "❌ 每日对账已连续 24 小时失败：未能提取失败原因"
    assert "exit 1" in text


def test_http_error_status_adjacency_hits_and_rejects():
    st = civitai_bot._http_error_status
    assert st("503 Service Unavailable") == 503
    assert st("429 Rate Limited, retry after 30s") == 429
    assert st("504 Gateway Time-out") == 504
    assert st("HTTP 502 from upstream") == 502
    assert st("status=503") == 503
    assert st("503, url='https://civitai.com/api/v1/images'") == 503
    assert st("timed out after 500 ms") is None
    assert st("TimeoutError while saving 503 items") is None
    assert st("503 items") is None
    assert st("500 ms") is None


def test_http_status_regex_boundary_anchors():
    """40-char non-digit gap hits; 41 rejects; post-phrase; leftmost pairable."""
    st = civitai_bot._http_error_status
    h = civitai_bot._humanize_scan_error
    # Word boundary after the code, then a 40-char non-digit gap to the phrase: space + 39 x == 40.
    assert st("503 " + "x" * 39 + "Service Unavailable") == 503
    assert st("503 " + "x" * 40 + "Service Unavailable") is None
    # Phrase-before (post group), e.g. nginx/upstream "Bad Gateway (502)".
    assert st("Bad Gateway (502)") == 502
    # Same line, two pairable codes: search() takes the leftmost match.
    assert st("502 Server Error then 503 Service Unavailable") == 502
    # Leftmost number is not pairable; first pairable code wins.
    assert st("processed 502 items; 503 Service Unavailable") == 503
    # A digit in the gap breaks adjacency; title falls back to the excerpt.
    interrupted = "503 (attempt 3): Service Unavailable"
    assert st(interrupted) is None
    assert "Civitai API" not in h(interrupted)
    assert h(interrupted) == interrupted


def test_recon_window_uses_own_503_and_ignores_scan_lines(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "t0 [WARNING] civitai-monitor: Page query failed (track=SFW): "
        "502 Server Error: Bad Gateway for url: https://civitai.com/api/v1/images\n"
        "t1 [INFO] civitai-bot: Daily deep reconciliation starting (03:30 target)...\n"
        "t2 [INFO] civitai-monitor: ── [RECONCILE] SFW track for @user ──\n"
        "t3 [WARNING] civitai-monitor: Page query failed (track=SFW): "
        "503 Server Error: Service Unavailable for url: https://civitai.com/api/v1/images\n"
        "t4 [WARNING] civitai-bot: Daily reconciliation finished with code 2 (will retry)\n"
        "t5 [WARNING] civitai-monitor: Page query failed (track=NSFW): "
        "500 Server Error: Internal Server Error for url: https://civitai.com/api/v1/images\n",
        encoding="utf-8",
    )
    raw = civitai_bot._last_recon_error_line(str(log))
    assert "503" in raw
    assert "502" not in raw
    assert "500 Server Error" not in raw
    headline = civitai_bot._cron_failure_headline(
        "reconciliation",
        "reconcile 非零退出（exit 2），今日第 1/10 次重试",
        log_path=str(log),
    )
    assert headline == f"Civitai API 503 {HTTPStatus.SERVICE_UNAVAILABLE.phrase}"
    # Scan extractor still sees the latest (post-window) scan line.
    scan_raw = civitai_bot._last_scan_error_line(str(log))
    assert "500" in scan_raw


def test_recon_headline_generic_when_log_has_only_scan_errors(tmp_path):
    log = tmp_path / "bot.log"
    log.write_text(
        "t0 [WARNING] civitai-monitor: Page query failed (track=SFW): "
        "503 Server Error: Service Unavailable for url: https://civitai.com/api/v1/images\n",
        encoding="utf-8",
    )
    assert civitai_bot._last_recon_error_line(str(log)) == ""
    headline = civitai_bot._cron_failure_headline(
        "reconciliation",
        "reconcile 非零退出（exit 1），今日第 1/3 次重试",
        log_path=str(log),
    )
    assert headline == "未能提取失败原因"


def test_recon_headline_from_reconcile_marker_without_start_line(tmp_path):
    """No start marker: fallback keeps only lines with a [RECONCILE] prefix.

    Production fetch errors (civitai_client.py) are
    ``Page query failed (track=...): ...`` with no ``[RECONCILE]`` prefix, so
    the no-start fallback cannot pick them. This is production behavior: the
    title degrades to 「未能提取失败原因」 rather than inventing a prefixed line.
    """
    log = tmp_path / "bot.log"
    log.write_text(
        "t0 [WARNING] civitai-monitor: Page query failed (track=SFW): "
        "502 Server Error: Bad Gateway for url: https://civitai.com/api/v1/images\n"
        "t1 [INFO] civitai-monitor: ── [RECONCILE] SFW track for @user ──\n"
        "t2 [WARNING] civitai-monitor: Page query failed (track=SFW): "
        "503 Server Error: Service Unavailable for url: https://civitai.com/api/v1/images\n",
        encoding="utf-8",
    )
    raw = civitai_bot._last_recon_error_line(str(log))
    assert raw == ""
    headline = civitai_bot._cron_failure_headline(
        "reconciliation",
        "reconcile 非零退出（exit 2）",
        log_path=str(log),
    )
    assert headline == "未能提取失败原因"


def test_recon_lock_contention_warning_is_tier3_headline(tmp_path):
    """Known limitation: colliding-scan lock WARNING can title a killed recon.

    The realistic way a lock-contention WARNING enters the recon window
    is a colliding scan tick (empirically 2026-09-14 03:31:45) while recon
    is running. The realistic way that line becomes the title is recon
    being killed with no logs (OOM/SIGKILL, negative exit such as -9):
    recon lock itself exits 75 and the scheduler does not report a failure,
    and exit 2 always leaves a tier-1 ``fetch_page failed`` ERROR in the
    same window. The lock line is the scan tick's lock contention, not the
    recon death cause. Title-best-effort accepts this misleading headline
    rather than inventing a better one.
    """
    log = tmp_path / "bot.log"
    log.write_text(
        "t0 [INFO] civitai-bot: Daily deep reconciliation starting (03:30 target)...\n"
        "t1 [WARNING] civitai-monitor: "
        "Another monitor process is already running - skipping this cron tick\n"
        "t2 [WARNING] civitai-bot: Daily reconciliation finished with code -9 (will retry)\n",
        encoding="utf-8",
    )
    raw = civitai_bot._last_recon_error_line(str(log))
    assert raw == (
        "Another monitor process is already running - skipping this cron tick"
    )
    headline = civitai_bot._cron_failure_headline(
        "reconciliation",
        "reconcile 非零退出（exit -9），今日第 1/10 次重试",
        log_path=str(log),
    )
    assert headline == (
        "Another monitor process is already running - skipping this cron tick"
    )


def test_recon_schedule_exception_headline_keeps_exception():
    detail = "对账调度异常：RuntimeError: boom（今日第 1 次重试）"
    headline = civitai_bot._cron_failure_headline(
        "reconciliation", detail, log_path=str(Path("/no/such/recon.log"))
    )
    assert headline == detail
    assert "未能提取失败原因" not in headline


@pytest.mark.asyncio
async def test_reconciliation_nonzero_exit_title_names_503(gate_env, monkeypatch):
    monkeypatch.setattr(
        civitai_bot,
        "_last_recon_error_line",
        lambda _p=None: (
            "Page query failed (track=SFW): 503 Server Error: "
            "Service Unavailable for url: https://civitai.com/api/v1/images"
        ),
    )
    monkeypatch.setattr(
        civitai_bot,
        "_last_scan_error_line",
        lambda _p=None: "Page query failed (track=NSFW): 502 Server Error: Bad Gateway",
    )
    civitai_bot._cron_alert_disk_loaded = True
    civitai_bot._cron_alert_state["reconciliation"] = {
        "last_success_ts": T0,
        "alerted": False,
        "last_alert_date": "",
    }
    await civitai_bot._report_cron_outcome(
        "reconciliation",
        failing=True,
        detail="reconcile 非零退出（exit 2），今日第 1/3 次重试",
    )
    text = gate_env["bot"].sent[0][1]
    first_line = text.split("\n", 1)[0]
    assert first_line == (
        f"❌ 每日对账已连续 24 小时失败：Civitai API 503 {HTTPStatus.SERVICE_UNAVAILABLE.phrase}"
    )
    assert "Civitai API 502" not in text
    assert "exit 2" in text


@pytest.mark.asyncio
async def test_reconciliation_schedule_exception_headline(gate_env, monkeypatch):
    monkeypatch.setattr(civitai_bot, "_last_recon_error_line", lambda _p=None: "")
    civitai_bot._cron_alert_disk_loaded = True
    civitai_bot._cron_alert_state["reconciliation"] = {
        "last_success_ts": T0,
        "alerted": False,
        "last_alert_date": "",
    }
    await civitai_bot._report_cron_outcome(
        "reconciliation",
        failing=True,
        detail="对账调度异常：RuntimeError: boom（今日第 1 次重试）",
    )
    text = gate_env["bot"].sent[0][1]
    first_line = text.split("\n", 1)[0]
    assert first_line == "❌ 每日对账已连续 24 小时失败：对账调度异常：RuntimeError: boom（今日第 1 次重试）"
    assert "未能提取失败原因" not in text


def test_persist_failure_rate_limited_then_recovery_info(gate_env, monkeypatch, caplog):
    """First persist failure is WARNING; repeats within 24h are debug; success logs info."""
    now_holder = {"t": 1_700_000_000.0}
    monkeypatch.setattr(civitai_bot.time, "time", lambda: now_holder["t"])
    real_write = civitai_bot._atomic_write

    def _boom(*_a, **_k):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(civitai_bot, "_atomic_write", _boom)
    with caplog.at_level(logging.DEBUG, logger="civitai-bot"):
        civitai_bot._persist_cron_alert_state()
        civitai_bot._persist_cron_alert_state()
        now_holder["t"] += civitai_bot.CRON_ALERT_WINDOW_S - 1
        civitai_bot._persist_cron_alert_state()
    warn = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Failed to persist cron alert state" in r.getMessage()
    ]
    debug = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "Failed to persist cron alert state" in r.getMessage()
    ]
    assert len(warn) == 1
    assert len(debug) == 2
    assert any(r.exc_info for r in warn)

    # Exactly 24h after the first WARNING, a repeat persist failure is WARNING again.
    caplog.clear()
    now_holder["t"] += 1  # first warning + CRON_ALERT_WINDOW_S (86400)
    with caplog.at_level(logging.DEBUG, logger="civitai-bot"):
        civitai_bot._persist_cron_alert_state()
    warn_at_24h = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "Failed to persist cron alert state" in r.getMessage()
    ]
    debug_at_24h = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG and "Failed to persist cron alert state" in r.getMessage()
    ]
    assert len(warn_at_24h) == 1
    assert len(debug_at_24h) == 0
    assert any(r.exc_info for r in warn_at_24h)

    caplog.clear()
    monkeypatch.setattr(civitai_bot, "_atomic_write", real_write)
    with caplog.at_level(logging.INFO, logger="civitai-bot"):
        civitai_bot._persist_cron_alert_state()
    assert any(
        r.levelno == logging.INFO and r.getMessage() == "告警状态持久化已恢复"
        for r in caplog.records
    )
    # A second success must not repeat the recovery notice.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="civitai-bot"):
        civitai_bot._persist_cron_alert_state()
    assert not any("告警状态持久化已恢复" in r.getMessage() for r in caplog.records)
