"""Stateful persistence, locking, alert gates, and process-status helpers for civitai-bot."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from telegram.ext import Application, ContextTypes

from civitai_monitor.bot_errors import (
    _CRON_ALERT_LABELS,
    _cron_failure_headline,
    _is_transient_telegram_error,
)
from monitor import MonitorConfig
from state_store import _atomic_write


def track_task(bot: Any, task: asyncio.Task) -> asyncio.Task:
    bot._background_tasks.add(task)
    task.add_done_callback(bot._background_tasks.discard)
    return task


async def shutdown_background_tasks(bot: Any, application: Application) -> None:
    pending = [t for t in bot._background_tasks if not t.done()]
    if not pending:
        return
    for t in pending:
        t.cancel()
    try:
        await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        pass


async def error_handler(bot: Any, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error

    if _is_transient_telegram_error(exc):
        bot.log.warning(
            "Transient Telegram network error (will retry; not alerting): %s: %s",
            type(exc).__name__,
            exc,
        )
        return

    if exc is not None:
        bot.log.error("Unhandled exception in update handler (update=%r)", update, exc_info=exc)
    else:
        bot.log.error("Unhandled exception in update handler (update=%r); no error attached", update)

    try:
        if not bot._ADMIN_CHAT_IDS or exc is None:
            return
        now = time.time()
        key = type(exc).__name__
        if now - bot._last_error_alert.get(key, 0.0) < bot._ERROR_ALERT_INTERVAL:
            return
        bot._last_error_alert[key] = now
        text = (
            "⚠️ Bot 未处理异常\n"
            f"类型: {type(exc).__name__}\n"
            f"信息: {str(exc)[:500]}"
        )
        for cid in bot._ADMIN_CHAT_IDS:
            try:
                await context.bot.send_message(chat_id=cid, text=text)
            except Exception:
                bot.log.debug("Failed to deliver error alert to admin %s", cid, exc_info=True)
    except Exception:
        bot.log.debug("error_handler: admin-alert dispatch failed", exc_info=True)


def cron_alert_state_path(bot: Any) -> Path:
    raw = os.environ.get("CIVITAI_CRON_ALERT_STATE")
    if raw:
        return Path(raw)
    return bot.SCRIPT_DIR / "cron_alert_state.json"


def read_cron_alert_state_file(bot: Any) -> dict[str, dict]:
    path = bot._cron_alert_state_path()
    try:
        if not path.exists() or path.stat().st_size == 0:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for job, st in data.items():
        if not isinstance(job, str) or not isinstance(st, dict):
            continue
        try:
            ts = float(st.get("last_success_ts"))
        except (TypeError, ValueError):
            continue
        out[job] = {
            "last_success_ts": ts,
            "alerted": bool(st.get("alerted", False)),
            "last_alert_date": str(st.get("last_alert_date") or ""),
        }
    return out


def persist_cron_alert_state(bot: Any) -> None:
    try:
        payload = json.dumps(bot._cron_alert_state, indent=2, sort_keys=True)
        getattr(bot, "_atomic_write", _atomic_write)(bot._cron_alert_state_path(), payload)
    except Exception:
        bot._cron_alert_persist_had_failure = True
        now = time.time()
        if (
            bot._cron_alert_persist_warned_at is None
            or now - bot._cron_alert_persist_warned_at >= bot.CRON_ALERT_WINDOW_S
        ):
            bot._cron_alert_persist_warned_at = now
            bot.log.warning("Failed to persist cron alert state", exc_info=True)
        else:
            bot.log.debug("Failed to persist cron alert state", exc_info=True)
        return
    if bot._cron_alert_persist_had_failure:
        bot._cron_alert_persist_had_failure = False
        bot._cron_alert_persist_warned_at = None
        bot.log.info("告警状态持久化已恢复")


def ensure_cron_alert_state_loaded(bot: Any) -> None:
    if bot._cron_alert_disk_loaded:
        return
    bot._cron_alert_disk_loaded = True
    disk = bot._read_cron_alert_state_file()
    for job, st in disk.items():
        bot._cron_alert_state.setdefault(job, st)


def job_alert_state(bot: Any, job: str, now: float) -> dict:
    bot._ensure_cron_alert_state_loaded()
    if job not in bot._cron_alert_state:
        bot._cron_alert_state[job] = {
            "last_success_ts": now,
            "alerted": False,
            "last_alert_date": "",
        }
        bot._persist_cron_alert_state()
    return bot._cron_alert_state[job]


def cron_alert_gate(
    bot: Any,
    job: str,
    *,
    failing: bool,
    now: float | None = None,
) -> str | None:
    if now is None:
        now = time.time()
    today = bot.datetime.fromtimestamp(now, bot.timezone.utc).strftime("%Y-%m-%d")
    state = bot._job_alert_state(job, now)
    try:
        last_success = float(state.get("last_success_ts"))
    except (TypeError, ValueError):
        last_success = now
        state["last_success_ts"] = now
        bot._persist_cron_alert_state()

    if failing:
        if now - last_success < bot.CRON_ALERT_WINDOW_S:
            return None
        if not state.get("alerted"):
            state["alerted"] = True
            state["last_alert_date"] = today
            bot._persist_cron_alert_state()
            return "fail"
        if state.get("last_alert_date") != today:
            state["last_alert_date"] = today
            bot._persist_cron_alert_state()
            return "digest"
        return None

    was_alerted = bool(state.get("alerted"))
    state["last_success_ts"] = now
    state["alerted"] = False
    state["last_alert_date"] = ""
    bot._persist_cron_alert_state()
    if was_alerted:
        return "recover"
    return None


async def report_cron_outcome(
    bot: Any,
    job: str,
    *,
    failing: bool,
    detail: str = "",
) -> None:
    try:
        kind = bot._cron_alert_gate(job, failing=failing)
        if kind is None:
            return
        label = _CRON_ALERT_LABELS.get(job, job)
        if kind == "recover":
            text = f"✅ {label}已恢复正常"
        else:
            headline = _cron_failure_headline(
                job,
                detail,
                scan_err_fn=bot._last_scan_error_line,
                recon_err_fn=bot._last_recon_error_line,
            )
            if kind == "fail":
                head = f"❌ {label}已连续 24 小时失败：{headline}"
            else:
                head = f"❌ {label}仍在失败（每日摘要）：{headline}"
            text = f"{head}\n{detail}" if detail else head
        tg_bot = bot._alert_bot
        if tg_bot is None or not bot._ADMIN_CHAT_IDS:
            bot.log.debug("Cron alert skipped (no bot/admin chats configured): %s", text)
            return
        for cid in bot._ADMIN_CHAT_IDS:
            try:
                await tg_bot.send_message(chat_id=cid, text=text)
            except Exception:
                bot.log.debug("Failed to deliver cron alert to admin %s", cid, exc_info=True)
    except Exception:
        bot.log.debug("Cron alert dispatch failed", exc_info=True)


def load_active_backfills(bot: Any) -> dict[str, dict[str, str]]:
    try:
        return json.loads(bot.ACTIVE_BACKFILLS.read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def save_active_backfills(bot: Any, data: dict[str, dict[str, str]]) -> None:
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w", suffix=".tmp", dir=bot.SCRIPT_DIR, delete=False, encoding="utf-8"
    )
    try:
        json.dump(data, tmp)
        tmp.close()
        os.replace(tmp.name, str(bot.ACTIVE_BACKFILLS))
    except Exception as e:
        bot.log.error("Failed to save active_backfills.json: %s", e)
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        raise


def acquire_backfill_lock(bot: Any, tg_id: str, username: str) -> tuple[int, Path] | None:
    lock_path = bot.SCRIPT_DIR / f".backfill_lock_{tg_id}_{username}.lck"
    fd = None
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, lock_path
    except OSError:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                bot.log.exception("Failed to close lock fd")
        return None


def release_backfill_lock(fd: int, path: Path) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass


def get_backfill_serial_lock(bot: Any) -> asyncio.Lock:
    if bot._backfill_serial_lock is None:
        bot._backfill_serial_lock = bot.asyncio.Lock()
    return bot._backfill_serial_lock


def register_backfill(bot: Any, tg_id: str, username: str) -> None:
    data = bot._load_active_backfills()
    if tg_id not in data:
        data[tg_id] = {}
    data[tg_id][username] = bot.datetime.now(bot.timezone.utc).isoformat()
    bot._save_active_backfills(data)


def unregister_backfill(bot: Any, tg_id: str, username: str) -> None:
    data = bot._load_active_backfills()
    if tg_id in data and username in data[tg_id]:
        del data[tg_id][username]
        if not data[tg_id]:
            del data[tg_id]
        bot._save_active_backfills(data)


def load_interval(bot: Any) -> int:
    try:
        data = json.loads(bot.INTERVAL_CONFIG.read_text())
    except FileNotFoundError:
        bot.log.info("No interval config at %s, using default 600s", bot.INTERVAL_CONFIG)
        return 600
    except (json.JSONDecodeError, OSError) as e:
        bot.log.warning("Invalid interval config, using default 600s: %s", e)
        return 600
    seconds = data.get("seconds", 600) if isinstance(data, dict) else None
    if isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0:
        return seconds
    bot.log.warning(
        "Invalid interval config (top-level %s, seconds=%r), using default 600s",
        type(data).__name__, seconds,
    )
    return 600


def save_interval(bot: Any, seconds: int) -> None:
    tmp = bot.INTERVAL_CONFIG.with_suffix(bot.INTERVAL_CONFIG.suffix + ".tmp")
    with open(tmp, "w") as f:
        f.write(json.dumps({"seconds": seconds}))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(bot.INTERVAL_CONFIG)
    bot.INTERVAL_CONFIG.chmod(0o600)


def reconciliation_status_file(bot: Any, cfg: MonitorConfig | None = None) -> Path:
    if cfg is None:
        cfg = bot.read_config()
    data_dir = Path(cfg.data.data_dir) if cfg.data.data_dir else bot.SCRIPT_DIR
    return data_dir / "reconciliation_status.json"


def load_reconciliation_last_success(bot: Any, cfg: MonitorConfig | None = None) -> str | None:
    path = bot._reconciliation_status_file(cfg)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("last_success_date")
    except (OSError, json.JSONDecodeError):
        return None


def save_reconciliation_last_success(
    bot: Any,
    date_str: str,
    cfg: MonitorConfig | None = None,
) -> None:
    path = bot._reconciliation_status_file(cfg)
    getattr(bot, "_atomic_write", _atomic_write)(path, json.dumps({"last_success_date": date_str}, indent=2))


def is_scan_running(bot: Any) -> bool:
    try:
        lock_path = bot.SCRIPT_DIR / ".monitor.lock"
        if not lock_path.exists():
            return False
        fd = os.open(str(lock_path), os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(fd)
            return False
        except (BlockingIOError, OSError):
            os.close(fd)
            return True
    except (BlockingIOError, OSError):
        return False


def kill_running_scan(bot: Any) -> list[int]:
    killed = []
    for proc in (bot._current_scan_proc, bot._current_recon_proc):
        if proc is None or proc.returncode is not None:
            continue
        pid = proc.pid
        try:
            proc.terminate()
            killed.append(pid)
        except ProcessLookupError:
            pass
        except OSError as e:
            bot.log.warning("Failed to terminate scan PID %s: %s", pid, e)
    return killed


def read_scan_status(bot: Any) -> str:
    if not bot._is_scan_running():
        return ""
    try:
        path = bot.SCRIPT_DIR / "monitor_status.json"
        elapsed = 0
        if path.exists():
            data = json.loads(path.read_text())
            elapsed = data.get("elapsed_seconds", 0)
        lines = ["⏳ 定时扫描运行中"]
        if elapsed >= 60:
            lines.append(f"已运行 {elapsed // 60} 分钟")
        else:
            lines.append(f"已运行 {elapsed} 秒")
        lines.append("")
        lines.append("输入 /stop 终止扫描后即可 /backfill")
        return "\n".join(lines)
    except Exception:
        bot.log.exception("Error generating status message (non-critical)")
        return ""


def sweep_stale_backfills(bot: Any, max_age_minutes: int) -> int:
    data = bot._load_active_backfills()
    if not data:
        return 0
    now = bot.datetime.now(bot.timezone.utc)
    removed = 0
    for tg_id, users_dict in list(data.items()):
        for username, last_active_str in list(users_dict.items()):
            try:
                last_active = bot.datetime.fromisoformat(last_active_str)
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=bot.timezone.utc)
                age_minutes = (now - last_active).total_seconds() / 60
                if age_minutes > max_age_minutes:
                    bot.log.warning(
                        "Stale backfill detected: @%s (tg=%s) age=%.0f min — unregistering",
                        username, tg_id, age_minutes,
                    )
                    bot._unregister_backfill(tg_id, username)
                    removed += 1
            except Exception as e:
                bot.log.exception("Could not parse last_active %r for @%s: %s", last_active_str, username, e)  # noqa: TRY401
    return removed
