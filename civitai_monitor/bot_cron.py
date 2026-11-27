"""Scheduled cron loops and weekly report builder for civitai-bot."""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from monitor import MonitorConfig
from state_store import load_push_history

_WEEKLY_REPORT_WEEKDAY = 6
_WEEKLY_REPORT_HOUR_UTC = 21
_WEEKLY_REPORT_MINUTE_UTC = 0
_WEEKLY_REPORT_WINDOW_DAYS = 7
_WEEKLY_REPORT_STALE_DAYS = 14


def weekly_report_due(now: datetime, last_sent_date: str | None) -> bool:
    """True when the weekly report should fire at ``now`` (aware UTC datetime)."""
    if now.weekday() != _WEEKLY_REPORT_WEEKDAY:
        return False
    if (now.hour, now.minute) < (_WEEKLY_REPORT_HOUR_UTC, _WEEKLY_REPORT_MINUTE_UTC):
        return False
    return last_sent_date != now.strftime("%Y-%m-%d")


def collect_weekly_creator_stats(
    bot: Any,
    cfg: MonitorConfig | None = None,
) -> list[dict]:
    """Per-subscription push activity over the report window."""
    if cfg is None:
        cfg = bot.read_config()
    data_dir = Path(cfg.data.data_dir) if cfg.data.data_dir else bot.SCRIPT_DIR
    pushed_dir = data_dir / "seen_ids"
    cutoff = time.time() - _WEEKLY_REPORT_WINDOW_DAYS * 86400
    stats: list[dict] = []
    subs = cfg.subscriptions or {}
    for tg_id, raw_users in subs.items():
        for u in raw_users or []:
            username = u.get("name", str(u)) if isinstance(u, dict) else str(u)
            history = load_push_history(pushed_dir, tg_id, username)
            stats.append(
                {
                    "username": username,
                    "tg_id": tg_id,
                    "pushes_7d": sum(1 for ts in history.values() if ts >= cutoff),
                    "last_push_ts": max(history.values(), default=None),
                }
            )
    stats.sort(key=lambda s: s["username"].lower())
    return stats


def format_last_push_date(last_push_ts: float | None) -> str:
    if last_push_ts is None:
        return "无记录"
    return datetime.fromtimestamp(last_push_ts, tz=timezone.utc).strftime("%Y-%m-%d")


def build_weekly_report(
    scan_stats: dict[str, int],
    creator_stats: list[dict],
    recon_last_success: str | None,
    *,
    now: datetime | None = None,
) -> str:
    """Assemble the weekly report message (pure function; unit-tested)."""
    if now is None:
        now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=_WEEKLY_REPORT_WINDOW_DAYS)
    lines = [
        "📅 每周监控日报",
        f"统计区间: {week_start.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')} (UTC)",
        "",
        "🗂 扫描健康（过去 7 天）",
        f"✅ 成功: {scan_stats.get('success', 0)} 次",
        f"❌ 失败: {scan_stats.get('failure', 0)} 次",
        f"⏭️ 跳过: {scan_stats.get('skipped', 0)} 次",
        "",
        "👥 订阅活跃度（近 7 天推送数 / 最近推送）",
    ]
    if not creator_stats:
        lines.append("• （当前无订阅）")
    for s in creator_stats:
        last_ts = s.get("last_push_ts")
        stale = last_ts is None or (
            now.timestamp() - last_ts > _WEEKLY_REPORT_STALE_DAYS * 86400
        )
        flag = " ⚠️ 超过 14 天未推送（可能上游停更或镜像失效，请人工确认）" if stale else ""
        lines.append(
            f"• @{s['username']}: 近 7 天 {s.get('pushes_7d', 0)} 条，"
            f"最近推送 {format_last_push_date(last_ts)}{flag}"
        )
    lines.append("")
    lines.append("🔄 深度对账")
    if recon_last_success:
        lines.append(f"最近成功: {recon_last_success}")
    else:
        lines.append("最近成功: 无记录 ⚠️")
    return "\n".join(lines)


async def send_weekly_report(bot: Any) -> bool:
    """Build + send the weekly report. Returns True when a send was recorded."""
    tg_bot = bot._alert_bot
    if tg_bot is None or not bot._ADMIN_CHAT_IDS:
        bot.log.debug("Weekly report skipped (no bot/admin chats configured)")
        return False
    cfg = bot.read_config()
    creator_stats = bot._collect_weekly_creator_stats(cfg)
    recon_date = bot._load_reconciliation_last_success(cfg)
    text = bot._build_weekly_report(dict(bot._scan_stats), creator_stats, recon_date)
    delivered = 0
    for cid in bot._ADMIN_CHAT_IDS:
        try:
            await tg_bot.send_message(chat_id=cid, text=text)
            delivered += 1
        except Exception:
            bot.log.warning("Failed to deliver weekly report to admin %s", cid, exc_info=True)
    if delivered == 0:
        bot.log.warning("Weekly report could not be delivered to any admin; will retry")
        return False
    return True


async def scheduled_weekly_report_cron(bot: Any) -> None:
    """Send the weekly activity report every Sunday at/after 21:00 UTC."""
    try:
        while not bot._shutdown_requested:
            now = bot.datetime.now(bot.timezone.utc)
            if bot._weekly_report_due(now, bot._weekly_report_last_sent_date):
                try:
                    sent = await bot._send_weekly_report()
                except Exception:
                    bot.log.exception("Weekly report send failed; will retry next tick")
                else:
                    if sent:
                        bot._weekly_report_last_sent_date = now.strftime("%Y-%m-%d")
                        bot._reset_scan_stats()
                        bot.log.info("Weekly report sent; scan counters reset")
            if bot._shutdown_requested:
                break
            await bot.asyncio.sleep(30)
    finally:
        bot.log.info("Weekly report cron stopped.")


async def scheduled_scan_cron(bot: Any) -> None:
    """Run monitor.py on a configurable interval as a background task."""
    cfg = bot.read_config()
    stale_backfill_minutes = cfg.backfill.stale_backfill_minutes
    proc: asyncio.subprocess.Process | None = None
    try:
        while not bot._shutdown_requested:
            bot._sweep_stale_backfills(stale_backfill_minutes)
            current_interval = bot._load_interval()
            active_backfills = bot._load_active_backfills()
            if active_backfills:
                bot.log.info(
                    "Scheduled scan skipped: active backfill(s) %s",
                    list(active_backfills.keys()),
                )
                await bot.asyncio.sleep(current_interval)
                continue
            try:
                bot.log.info("Scheduled scan starting...")
                proc = await bot.asyncio.create_subprocess_exec(
                    sys.executable, str(bot.MONITOR_SCRIPT),
                    cwd=str(bot.SCRIPT_DIR),
                )
                bot._current_scan_proc = proc
                rc = await proc.wait() if proc is not None else None
                bot._current_scan_proc = None
                if bot._shutdown_requested:
                    break
                returncode = int(rc) if rc is not None else -1
                if returncode == 0:
                    bot.log.info("Scheduled scan completed")
                    bot._scan_stats["success"] += 1
                    await bot._report_cron_outcome("scan", failing=False)
                elif returncode == 75:
                    bot.log.info("Scheduled scan skipped: monitor lock held (backfill in progress)")
                    bot._scan_stats["skipped"] += 1
                else:
                    bot.log.warning("Scheduled scan failed (exit %d)", returncode)
                    bot._scan_stats["failure"] += 1
                    detail = f"monitor.py 非零退出（exit {returncode}），本轮扫描可能未完成；将按当前间隔自动重试"
                    reason = bot._last_scan_error_line()
                    if reason:
                        detail += f"\n原因: {reason}"
                    await bot._report_cron_outcome(
                        "scan",
                        failing=True,
                        detail=detail,
                    )
            except Exception as e:
                bot.log.exception("Scheduled scan error: %s", e)  # noqa: TRY401
                bot._scan_stats["failure"] += 1
                await bot._report_cron_outcome(
                    "scan",
                    failing=True,
                    detail=f"扫描调度异常：{type(e).__name__}: {e}",
                )
            if bot._shutdown_requested:
                break
            await bot.asyncio.sleep(current_interval)
    finally:
        if proc is not None and proc.returncode is None:
            bot.log.info("Shutdown: stopping current scan...")
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await bot.asyncio.wait_for(proc.wait(), timeout=30)
                bot.log.info("Shutdown: scan exited cleanly (exit %d)", proc.returncode)
            except RuntimeError:
                bot.log.info("Shutdown: event loop already closed, leaving scan to OS reaper")
            except asyncio.TimeoutError:
                bot.log.warning("Shutdown: scan did not exit in 30s, sending SIGKILL")
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except RuntimeError:
                    pass
        bot.log.info("Scheduled scan stopped. Returning to event loop for clean shutdown.")


async def scheduled_reconciliation_cron(bot: Any) -> None:
    """Run daily deep reconciliation as a background task."""
    proc: asyncio.subprocess.Process | None = None
    retry_date: str | None = None
    daily_retry_count: int = 0
    max_daily_retries = 10

    try:
        while not bot._shutdown_requested:
            cfg = bot.read_config()
            recon_cfg = getattr(cfg, "reconciliation", None)
            if recon_cfg is None or not recon_cfg.enabled:
                await bot.asyncio.sleep(60)
                continue

            time_str = recon_cfg.time or "03:30"
            try:
                target_hour, target_minute = [int(x) for x in time_str.split(":", 1)]
            except ValueError:
                target_hour, target_minute = 3, 30
            if not (0 <= target_hour <= 23 and 0 <= target_minute <= 59):
                bot.log.warning(
                    "Invalid reconciliation time %r (need HH:MM, 00-23:00-59); falling back to 03:30",
                    time_str,
                )
                target_hour, target_minute = 3, 30

            now = bot.datetime.now(bot.timezone.utc)
            today_str = now.strftime("%Y-%m-%d")
            target_dt = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
            last_success = bot._load_reconciliation_last_success(cfg)

            if now >= target_dt and last_success != today_str:
                if retry_date != today_str:
                    retry_date = today_str
                    daily_retry_count = 0

                if daily_retry_count >= max_daily_retries:
                    bot.log.warning(
                        "Daily reconciliation exceeded max retries (%d) today (%s), skipping until tomorrow",
                        daily_retry_count, today_str,
                    )
                    await bot.asyncio.sleep(60)
                    continue

                active_backfills = bot._load_active_backfills()
                if active_backfills:
                    bot.log.info("Daily reconciliation skipped: active backfill(s) %s", list(active_backfills.keys()))
                    await bot.asyncio.sleep(60)
                    continue

                try:
                    bot.log.info("Daily deep reconciliation starting (%02d:%02d target)...", target_hour, target_minute)
                    proc = await bot.asyncio.create_subprocess_exec(
                        sys.executable, str(bot.MONITOR_SCRIPT),
                        "--mode", "reconcile",
                        cwd=str(bot.SCRIPT_DIR),
                    )
                    bot._current_recon_proc = proc
                    rc = await proc.wait() if proc is not None else None
                    bot._current_recon_proc = None
                    if bot._shutdown_requested:
                        break
                    returncode = int(rc) if rc is not None else -1
                    if returncode == 0:
                        bot.log.info("Daily deep reconciliation completed successfully")
                        bot._save_reconciliation_last_success(today_str, cfg)
                        daily_retry_count = 0
                        await bot._report_cron_outcome("reconciliation", failing=False)
                    elif returncode == 75:
                        bot.log.info("Daily reconciliation skipped: monitor lock held (will retry in 60s)")
                        await bot.asyncio.sleep(60)
                        continue
                    else:
                        bot.log.warning("Daily reconciliation finished with code %d (will retry)", returncode)
                        daily_retry_count += 1
                        await bot._report_cron_outcome(
                            "reconciliation",
                            failing=True,
                            detail=(
                                f"reconcile 非零退出（exit {returncode}），"
                                f"今日第 {daily_retry_count}/{max_daily_retries} 次重试"
                            ),
                        )
                        await bot.asyncio.sleep(60)
                        continue
                except Exception as e:
                    bot.log.exception("Daily reconciliation error: %s", e)  # noqa: TRY401
                    bot._current_recon_proc = None
                    daily_retry_count += 1
                    await bot._report_cron_outcome(
                        "reconciliation",
                        failing=True,
                        detail=f"对账调度异常：{type(e).__name__}: {e}（今日第 {daily_retry_count} 次重试）",
                    )
                    await bot.asyncio.sleep(60)
                    continue

            if bot._shutdown_requested:
                break
            await bot.asyncio.sleep(30)
    finally:
        if proc is not None and proc.returncode is None:
            bot.log.info("Shutdown: stopping current reconciliation scan...")
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await bot.asyncio.wait_for(proc.wait(), timeout=30)
            except (RuntimeError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        bot._current_recon_proc = None
        bot.log.info("Daily reconciliation cron stopped.")
