#!/usr/bin/env python3
"""
Civitai Monitor Admin Bot — manage your monitor via Telegram chat.

Thin orchestration facade: domain logic lives in ``civitai_monitor/bot_*.py``,
while this entrypoint preserves CLI invocation (`python3 civitai-bot.py`) and
module-level symbols expected by existing tests.
"""

from __future__ import annotations

import asyncio
import fcntl  # noqa: F401
import logging
import os  # noqa: F401
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone  # noqa: F401
from functools import wraps
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.ext import Application, ContextTypes

from bot_ui import paginated_user_keyboard, resolve_callback_username  # noqa: F401
from civitai_monitor import bot_commands as _commands
from civitai_monitor import bot_cron as _cron
from civitai_monitor import bot_errors as _errors
from civitai_monitor import bot_state as _state
from civitai_monitor.bot_cron import (
    _WEEKLY_REPORT_HOUR_UTC,  # noqa: F401
    _WEEKLY_REPORT_MINUTE_UTC,  # noqa: F401
    _WEEKLY_REPORT_STALE_DAYS,  # noqa: F401
    _WEEKLY_REPORT_WEEKDAY,  # noqa: F401
    _WEEKLY_REPORT_WINDOW_DAYS,  # noqa: F401
)
from civitai_monitor.bot_cron import (
    build_weekly_report as _build_weekly_report,  # noqa: F401
)
from civitai_monitor.bot_cron import (
    format_last_push_date as _format_last_push_date,  # noqa: F401
)
from civitai_monitor.bot_cron import (
    weekly_report_due as _weekly_report_due,  # noqa: F401
)
from civitai_monitor.bot_errors import (
    _BARE_EXIT_NOISE,  # noqa: F401
    _CONN_MARKERS,  # noqa: F401
    _CRON_ALERT_LABELS,  # noqa: F401
    _HTTP_ADJACENT_RE,  # noqa: F401
    _HTTP_ERROR_PHRASE,  # noqa: F401
    _HTTP_EXPLICIT_RE,  # noqa: F401
    _HTTP_STATUS_CODES,  # noqa: F401
    _RECON_END_MARKERS,  # noqa: F401
    _RECON_START_MARKER,  # noqa: F401
    _SCAN_ERROR_FALLBACK_MARKERS,  # noqa: F401
    _SCAN_ERROR_MARKERS,  # noqa: F401
    _TIMEOUT_MARKERS,  # noqa: F401
    _TRANSIENT_TG_ERRORS,  # noqa: F401
    _classify_failure,  # noqa: F401
    _http_error_status,  # noqa: F401
    _human_size,  # noqa: F401
    _humanize_scan_error,  # noqa: F401
    _is_transient_telegram_error,  # noqa: F401
    _last_recon_error_line,
    _last_scan_error_line,
    _log_tail_candidates,  # noqa: F401
    _looks_like_bare_exit,  # noqa: F401
    _pick_error_line,  # noqa: F401
    _recon_window_lines,  # noqa: F401
    _strip_log_prefix,  # noqa: F401
    _summarise_log,  # noqa: F401
)
from civitai_monitor.bot_helpers import (
    _CIVITAI_URL_PREFIXES,  # noqa: F401
    CIVITAI_API,  # noqa: F401
    USERNAME_RE,  # noqa: F401
    _application_builder_for_config,  # noqa: F401
    communicate_with_idle_timeout,  # noqa: F401
    get_users,  # noqa: F401
    parse_username_input,  # noqa: F401
    read_config_from,
    set_users,  # noqa: F401
)
from civitai_monitor.bot_helpers import (
    validate_username_exists as _validate_username_exists,
)
from monitor import (  # noqa: F401
    MonitorConfig,
    cleanup_old_caches,
    load_config,
    write_config,
)
from state_store import _atomic_write, load_push_history  # noqa: F401

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)
log = logging.getLogger("civitai-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Module state (preserved here so per-instance test monkeypatches work cleanly)
# ---------------------------------------------------------------------------

_background_tasks: set[asyncio.Task] = set()
_ADMIN_CHAT_IDS: list[int] = []
_alert_bot = None
_cron_alert_state: dict[str, dict] = {}
_cron_alert_disk_loaded = False
CRON_ALERT_WINDOW_S = 24 * 3600
_cron_alert_persist_had_failure = False
_cron_alert_persist_warned_at: float | None = None
_last_error_alert: dict[str, float] = {}
_ERROR_ALERT_INTERVAL = 300.0
_user_last_call: dict[str, float] = defaultdict(float)

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
DOWNLOAD_DIR = SCRIPT_DIR / "downloads"
MONITOR_SCRIPT = SCRIPT_DIR / "monitor.py"
INTERVAL_CONFIG = SCRIPT_DIR / "interval.json"
ACTIVE_BACKFILLS = SCRIPT_DIR / "active_backfills.json"

_backfill_serial_lock: asyncio.Lock | None = None
_scan_interval: int = 600
_current_scan_proc: asyncio.subprocess.Process | None = None
_current_recon_proc: asyncio.subprocess.Process | None = None
_shutdown_requested = False
AUTHORIZED_USER_IDS: set[int] = set()

_scan_stats: dict[str, int] = {"success": 0, "failure": 0, "skipped": 0}
_weekly_report_last_sent_date: str | None = None


def _bot() -> Any:
    return sys.modules[__name__]


def rate_limit(min_interval: float = 5.0):
    """Per-user rate limiter: at least min_interval seconds between calls."""
    def decorator(func):
        @wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            user = update.effective_user
            if user is None:
                return
            user_id = str(user.id)
            now = time.time()
            elapsed = now - _user_last_call.get(user_id, 0)
            if elapsed < min_interval:
                remaining = int(min_interval - elapsed) + 1
                notice = f"⏳ 请等 {remaining} 秒后再试"
                msg = update.effective_message
                if msg is not None:
                    await msg.reply_text(notice)
                elif update.callback_query is not None:
                    try:
                        await update.callback_query.answer(notice, show_alert=True)
                    except Exception:
                        log.debug("rate_limit: callback answer failed", exc_info=True)
                return
            _user_last_call[user_id] = now
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator


def read_config() -> MonitorConfig:
    return read_config_from(CONFIG_PATH)


def validate_username_exists(username: str, cookies_path: str | Path | None = None) -> tuple[bool, str]:
    return _validate_username_exists(username, cookies_path, default_script_dir=SCRIPT_DIR)


def _track_task(task: asyncio.Task) -> asyncio.Task:
    return _state.track_task(_bot(), task)


async def _shutdown_background_tasks(application: Application) -> None:
    await _state.shutdown_background_tasks(_bot(), application)


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _state.error_handler(_bot(), update, context)


def _cron_alert_state_path() -> Path:
    return _state.cron_alert_state_path(_bot())


def _read_cron_alert_state_file() -> dict[str, dict]:
    return _state.read_cron_alert_state_file(_bot())


def _persist_cron_alert_state() -> None:
    _state.persist_cron_alert_state(_bot())


def _ensure_cron_alert_state_loaded() -> None:
    _state.ensure_cron_alert_state_loaded(_bot())


def _job_alert_state(job: str, now: float) -> dict:
    return _state.job_alert_state(_bot(), job, now)


def _cron_failure_headline(
    job: str, detail: str = "", *, log_path: str | None = None
) -> str:
    return _errors._cron_failure_headline(
        job,
        detail,
        log_path=log_path,
        scan_err_fn=_last_scan_error_line,
        recon_err_fn=_last_recon_error_line,
    )


def _cron_alert_gate(job: str, *, failing: bool, now: float | None = None) -> str | None:
    return _state.cron_alert_gate(_bot(), job, failing=failing, now=now)


async def _report_cron_outcome(job: str, *, failing: bool, detail: str = "") -> None:
    await _state.report_cron_outcome(_bot(), job, failing=failing, detail=detail)


def _load_active_backfills() -> dict[str, dict[str, str]]:
    return _state.load_active_backfills(_bot())


def _save_active_backfills(data: dict[str, dict[str, str]]) -> None:
    _state.save_active_backfills(_bot(), data)


def _acquire_backfill_lock(tg_id: str, username: str) -> tuple[int, Path] | None:
    return _state.acquire_backfill_lock(_bot(), tg_id, username)


def _release_backfill_lock(fd: int, path: Path) -> None:
    _state.release_backfill_lock(fd, path)


def _get_backfill_serial_lock() -> asyncio.Lock:
    return _state.get_backfill_serial_lock(_bot())


def _register_backfill(tg_id: str, username: str) -> None:
    _state.register_backfill(_bot(), tg_id, username)


def _unregister_backfill(tg_id: str, username: str) -> None:
    _state.unregister_backfill(_bot(), tg_id, username)


def _load_interval() -> int:
    return _state.load_interval(_bot())


def _save_interval(seconds: int) -> None:
    _state.save_interval(_bot(), seconds)


def _reconciliation_status_file(cfg: MonitorConfig | None = None) -> Path:
    return _state.reconciliation_status_file(_bot(), cfg)


def _load_reconciliation_last_success(cfg: MonitorConfig | None = None) -> str | None:
    return _state.load_reconciliation_last_success(_bot(), cfg)


def _save_reconciliation_last_success(date_str: str, cfg: MonitorConfig | None = None) -> None:
    _state.save_reconciliation_last_success(_bot(), date_str, cfg)


def _is_scan_running() -> bool:
    return _state.is_scan_running(_bot())


def _kill_running_scan() -> list[int]:
    return _state.kill_running_scan(_bot())


def _read_scan_status() -> str:
    return _state.read_scan_status(_bot())


def _sweep_stale_backfills(max_age_minutes: int) -> int:
    return _state.sweep_stale_backfills(_bot(), max_age_minutes)


def _reset_scan_stats() -> None:
    global _scan_stats
    _scan_stats = {"success": 0, "failure": 0, "skipped": 0}


def _collect_weekly_creator_stats(cfg: MonitorConfig | None = None) -> list[dict]:
    return _cron.collect_weekly_creator_stats(_bot(), cfg)


async def _send_weekly_report() -> bool:
    return await _cron.send_weekly_report(_bot())


async def scheduled_weekly_report_cron() -> None:
    await _cron.scheduled_weekly_report_cron(_bot())


async def scheduled_scan_cron() -> None:
    await _cron.scheduled_scan_cron(_bot())


async def scheduled_reconciliation_cron() -> None:
    await _cron.scheduled_reconciliation_cron(_bot())


async def _check_auth(update: Update) -> bool:
    return await _commands.check_auth(_bot(), update)


@rate_limit(min_interval=3.0)
async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_help(_bot(), update, _ctx)


@rate_limit(min_interval=3.0)
async def cmd_add(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_add(_bot(), update, _ctx)


@rate_limit(min_interval=3.0)
async def cmd_remove(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_remove(_bot(), update, _ctx)


async def _show_remove_list(message, telegram_user_id: int, page: int = 0) -> None:
    await _commands.show_remove_list(_bot(), message, telegram_user_id, page)


@rate_limit(min_interval=3.0)
async def cmd_remove_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_remove_callback(_bot(), update, _ctx)


async def _render_remove_page(query, users: list[str], page: int) -> None:
    await _commands.render_remove_page(_bot(), query, users, page)


@rate_limit(min_interval=3.0)
async def cmd_list(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_list(_bot(), update, _ctx)


@rate_limit(min_interval=3.0)
async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_status(_bot(), update, _ctx)


async def cmd_mode(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_mode(_bot(), update, _ctx)


async def cmd_nsfw(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_nsfw(_bot(), update, _ctx)


async def cmd_cleanup(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_cleanup(_bot(), update, _ctx)


@rate_limit(min_interval=10.0)
async def cmd_scan(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_scan(_bot(), update, _ctx)


async def cmd_interval(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_interval(_bot(), update, _ctx)


async def cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_stop(_bot(), update, _ctx)


@rate_limit(min_interval=10.0)
async def cmd_backfill(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_backfill(_bot(), update, _ctx)


async def _show_backfill_list(message, telegram_user_id: int, page: int = 0) -> None:
    await _commands.show_backfill_list(_bot(), message, telegram_user_id, page)


@rate_limit(min_interval=3.0)
async def cmd_backfill_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await _commands.cmd_backfill_callback(_bot(), update, _ctx)


async def _run_backfill(username: str, tg_uid: int) -> subprocess.CompletedProcess | None | str:
    return await _commands.run_backfill(_bot(), username, tg_uid)


def _resume_stale_backfills(application: Application) -> None:
    _commands.resume_stale_backfills(_bot(), application)


async def _resume_backfill_task(application: Application, tg_id: str, username: str) -> None:
    await _commands.resume_backfill_task(_bot(), application, tg_id, username)


async def _render_backfill_page(query, users: list[str], page: int) -> None:
    await _commands.render_backfill_page(_bot(), query, users, page)


async def post_init(application: Application) -> None:
    await _commands.post_init(_bot(), application)


def main() -> None:
    _commands.run_bot_main(_bot())


if __name__ == "__main__":
    main()
