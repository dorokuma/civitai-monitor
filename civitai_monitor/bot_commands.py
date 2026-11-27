"""Telegram command/callback handlers, backfill runner, and bot lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from telegram import BotCommand, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


async def check_auth(bot: Any, update: Update) -> bool:
    if update.effective_user and update.effective_user.id in bot.AUTHORIZED_USER_IDS:
        return True
    if update.message:
        await update.message.reply_text("⛔ 未授权，此 Bot 仅限主人使用")
    return False


async def cmd_help(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    text = (
        "🤖 *Civitai Monitor Admin Bot*\n\n"
        "`/add <username>` — Add a user to the watch list\n"
        "`/remove <username>` — Remove a user\n"
        "`/list` — List all watched users\n"
        "`/status` — Show monitor status\n"
        "`/mode <incremental|full>` — Switch scan mode\n"
        "`/nsfw <sfw_only|nsfw_only|both>` — Switch NSFW filter\n"
        "`/cleanup [days]` — Clean cached images older than N days\n"
        "`/scan` — Trigger an immediate incremental scan\n"
        "`/backfill <username>` — Full backfill for a user\n"
        "`/help` — Show this message"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_add(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "用法: `/add <用户名|主页链接|@用户名>`\n"
            "示例: `/add TargetUser`\n"
            "      `/add https://civitai.red/user/TargetUser`\n"
            "      `/add @TargetUser`",
            parse_mode="Markdown",
        )
        return

    username = bot.parse_username_input(args[1])
    if not username:
        await update.message.reply_text(
            "❌ 无法识别用户名。支持的格式：\n"
            "• 纯用户名: `TargetUser`\n"
            "• 主页链接: `https://civitai.com/user/xxx`\n"
            "• @用户名: `@TargetUser`",
            parse_mode="Markdown",
        )
        return

    if not bot.USERNAME_RE.match(username):
        await update.message.reply_text(
            f"❌ 用户名 `{username}` 格式无效（仅允许字母、数字、下划线、连字符和点，2-64位）",
            parse_mode="Markdown",
        )
        return

    verifying_msg = await update.message.reply_text(f"⏳ 正在验证 @{username} 是否存在...")

    cookies_file = bot.read_config().http.cookies_file or None
    ok, msg = await bot.asyncio.to_thread(bot.validate_username_exists, username, cookies_file)
    if not ok:
        await verifying_msg.edit_text(msg)
        return
    await verifying_msg.edit_text(msg)

    cfg = bot.read_config()
    uid = update.effective_user.id
    users = bot.get_users(cfg, uid)
    if any(u.casefold() == username.casefold() for u in users):
        await update.message.reply_text(f"👤 @{username} 已经在监控列表中了")
        return

    users.append(username)
    cfg = bot.set_users(cfg, uid, users)
    bot.write_config(cfg, bot.CONFIG_PATH)
    await update.message.reply_text(
        f"✅ 已添加 @{username} 到监控列表\n"
        f"下次定时任务（每10分钟）将自动开始抓取",
    )


async def show_remove_list(bot: Any, message, telegram_user_id: int, page: int = 0) -> None:
    cfg = bot.read_config()
    users = bot.get_users(cfg, telegram_user_id)
    if not users:
        await message.reply_text("📭 监控列表是空的，先 `/add` 加几个吧", parse_mode="Markdown")
        return

    markup, total_text, _page = bot.paginated_user_keyboard(
        users, page,
        item_prefix="rem",
        item_label_fmt="❌ @{u}",
        page_prefix="rem_pg",
        close_data="rem_cl",
    )
    await message.reply_text(
        f"{total_text}\n点击按钮取消关注：",
        reply_markup=markup,
    )


async def render_remove_page(bot: Any, query, users: list[str], page: int) -> None:
    markup, total_text, _page = bot.paginated_user_keyboard(
        users, page,
        item_prefix="rem",
        item_label_fmt="❌ @{u}",
        page_prefix="rem_pg",
        close_data="rem_cl",
    )
    await query.edit_message_text(
        f"{total_text}\n点击按钮取消关注：",
        reply_markup=markup,
    )


async def cmd_remove(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    await bot._show_remove_list(update.message, update.effective_user.id, page=0)


async def cmd_remove_callback(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    try:
        if data == "rem_cl":
            await query.edit_message_text("🔒 已关闭", reply_markup=None)
            return

        if data.startswith("rem_pg:"):
            page = int(data.split(":", 1)[1])
            cfg = bot.read_config()
            users = bot.get_users(cfg, uid)
            await bot._render_remove_page(query, users, page)
            return

        if data.startswith("rem:"):
            username = bot.resolve_callback_username(data.split(":", 1)[1])
            cfg = bot.read_config()
            users = bot.get_users(cfg, uid)
            if username not in users:
                await query.edit_message_text(f"👤 @{username} 已不在监控列表中", reply_markup=None)
                return
            users.remove(username)
            cfg = bot.set_users(cfg, uid, users)
            bot.write_config(cfg, bot.CONFIG_PATH)

            if users:
                await query.edit_message_text(f"✅ 已取消关注 @{username}", reply_markup=None)
                await bot._show_remove_list(query.message, uid, page=0)
            else:
                await query.edit_message_text(f"✅ 已取消关注 @{username}\n📭 监控列表已清空", reply_markup=None)
    except Exception as e:
        bot.log.exception("Remove callback error: %s", e)  # noqa: TRY401
        try:
            await query.edit_message_text("❌ 操作失败，请重试 /remove", reply_markup=None)
        except Exception as e:
            bot.log.exception("Nested error in remove callback: %s", e)  # noqa: TRY401


async def cmd_list(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    cfg = bot.read_config()
    users = bot.get_users(cfg, update.effective_user.id)
    if not users:
        await update.message.reply_text("📭 No users configured. Use `/add` to add some.", parse_mode="Markdown")
        return
    lines = [f"👤 {u}" for u in users]
    text = f"*Watched users ({len(lines)}):*\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_status(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    cfg = bot.read_config()
    users = bot.get_users(cfg, update.effective_user.id)

    seen_count = 0
    mtime = ""
    data_dir = Path(cfg.data.data_dir) if cfg.data.data_dir else bot.SCRIPT_DIR
    seen_dir = data_dir / "seen_ids"
    if seen_dir.exists():
        latest_mtime = 0
        for f in seen_dir.iterdir():
            if f.suffix == ".json":
                try:
                    data = json.loads(f.read_text())
                    seen_count += len(data) if isinstance(data, list) else 0
                    latest_mtime = max(latest_mtime, f.stat().st_mtime)
                except (OSError, ValueError) as e:
                    bot.log.warning("Error checking file mtime: %s", e)
        if latest_mtime:
            from zoneinfo import ZoneInfo
            mt = bot.datetime.fromtimestamp(latest_mtime, tz=bot.timezone.utc)
            bj = mt.astimezone(ZoneInfo("Asia/Shanghai"))
            mtime = bj.strftime("%Y-%m-%d %H:%M")

    download_size = 0
    if bot.DOWNLOAD_DIR.exists():
        download_size = sum(f.stat().st_size for f in bot.DOWNLOAD_DIR.iterdir() if f.is_file())

    size_str = bot._human_size(download_size)

    lines = [
        f"👥 监控 · {len(users)} 人",
        "",
        f"💾 缓存 · {size_str}",
        "",
        f"🕐 更新 · {mtime}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_mode(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/mode <incremental|full>`\n"
            "⚠️ *全局设置*：会影响**全部订阅者**的扫描模式。",
            parse_mode="Markdown",
        )
        return
    mode = args[1].strip().lower()
    if mode not in ("incremental", "full"):
        await update.message.reply_text("Invalid mode. Choose `incremental` or `full`.", parse_mode="Markdown")
        return
    cfg = bot.read_config()
    cfg.mode = mode
    bot.write_config(cfg, bot.CONFIG_PATH)
    await update.message.reply_text(
        f"✅ Mode set to `{mode}`.\n"
        f"⚠️ *全局设置*：此变更影响**全部订阅者**（不是仅你自己）。",
        parse_mode="Markdown",
    )


async def cmd_nsfw(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/nsfw <sfw_only|nsfw_only|both>`\n"
            "⚠️ *全局设置*：会影响**全部订阅者**的 NSFW 过滤。",
            parse_mode="Markdown",
        )
        return
    val = args[1].strip().lower()
    if val not in ("sfw_only", "nsfw_only", "both"):
        await update.message.reply_text("Invalid. Choose `sfw_only`, `nsfw_only`, or `both`.", parse_mode="Markdown")
        return
    cfg = bot.read_config()
    cfg.nsfw = val
    bot.write_config(cfg, bot.CONFIG_PATH)
    await update.message.reply_text(
        f"✅ NSFW filter set to `{val}`.\n"
        f"⚠️ *全局设置*：此变更影响**全部订阅者**（不是仅你自己）。",
        parse_mode="Markdown",
    )


async def cmd_cleanup(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    days = 7
    if len(args) >= 2:
        try:
            days = int(args[1])
        except ValueError:
            await update.message.reply_text("Usage: `/cleanup [days]` — days must be a number.", parse_mode="Markdown")
            return
        if days < 1:
            await update.message.reply_text(
                "Usage: `/cleanup [days]` — days 必须为 ≥ 1 的整数（0/负数会清空全部缓存，已拒绝）。",
                parse_mode="Markdown",
            )
            return

    if not bot.DOWNLOAD_DIR.exists():
        await update.message.reply_text("📂 Download directory does not exist — nothing to clean.")
        return

    cfg = bot.read_config()
    max_total_gb = getattr(cfg.download, "max_total_gb", 0) or 0
    before_files = []
    for root, _dirs, files in os.walk(bot.DOWNLOAD_DIR):
        for fname in files:
            fpath = Path(root) / fname
            try:
                before_files.append((fpath, fpath.stat().st_size))
            except OSError:
                pass
    before_size = sum(s for _, s in before_files)

    removed = bot.cleanup_old_caches(bot.DOWNLOAD_DIR, days, max_total_gb)

    after_size = 0
    if bot.DOWNLOAD_DIR.exists():
        for root, _dirs, files in os.walk(bot.DOWNLOAD_DIR):
            for fname in files:
                try:
                    after_size += (Path(root) / fname).stat().st_size
                except OSError:
                    pass
    size_freed = max(0, before_size - after_size)

    if removed:
        extra = f"，并按 max_total_gb={max_total_gb} 限额裁剪" if max_total_gb > 0 else ""
        await update.message.reply_text(
            f"🧹 Cleaned {removed} cached file(s) older than {days} days{extra} "
            f"(freed {bot._human_size(size_freed)})."
        )
    else:
        await update.message.reply_text(f"📂 No cached files older than {days} days.")


async def cmd_scan(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    await update.message.reply_text("🔍 Running incremental scan...")
    try:
        proc = await bot.asyncio.create_subprocess_exec(
            sys.executable, str(bot.MONITOR_SCRIPT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(bot.SCRIPT_DIR),
        )
        bot._current_scan_proc = proc
        try:
            _out, err = await bot.communicate_with_idle_timeout(proc, timeout=1800)
        except asyncio.TimeoutError:
            try:
                proc.terminate()
                await bot.asyncio.wait_for(proc.wait(), timeout=10)
            except (ProcessLookupError, asyncio.TimeoutError):
                proc.kill()
                await proc.wait()
            await update.message.reply_text("⏱ 扫描空闲超时（30 分钟无输出）已终止。")
            return
        finally:
            if bot._current_scan_proc is proc:
                bot._current_scan_proc = None
        stderr = err.decode("utf-8") if isinstance(err, bytes) else err
        if proc.returncode == 75:
            progress = bot._read_scan_status()
            msg = progress if progress else "⏳ 当前有定时扫描正在运行。"
            await update.message.reply_text(msg, parse_mode="Markdown")
            return
        if proc.returncode != 0:
            reason = bot._classify_failure(proc.returncode, stderr)
            await update.message.reply_text(f"❌ Scan failed: {reason}")
            return
        summary = bot._summarise_log(stderr)
        await update.message.reply_text(
            f"✅ Scan complete.\n{summary}",
            parse_mode="Markdown",
        )
    except Exception as e:
        bot.log.exception("Scan command error: %s", e)  # noqa: TRY401
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_interval(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/interval <minutes>`  e.g. `/interval 30`", parse_mode="Markdown"
        )
        return
    try:
        minutes = int(args[1])
        if minutes < 1 or minutes > 1440:
            await update.message.reply_text("Interval must be between 1 and 1440 minutes.")
            return
        seconds = minutes * 60
        bot._scan_interval = seconds
        bot._save_interval(seconds)
        await update.message.reply_text(f"✅ Scan interval set to {minutes} minutes.")
    except ValueError:
        await update.message.reply_text("Invalid number. Usage: `/interval <minutes>`", parse_mode="Markdown")


async def cmd_stop(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return

    procs_to_stop: list[tuple[asyncio.subprocess.Process, str]] = []
    if bot._current_scan_proc is not None and bot._current_scan_proc.returncode is None:
        procs_to_stop.append((bot._current_scan_proc, "定时增量扫描"))
    if bot._current_recon_proc is not None and bot._current_recon_proc.returncode is None:
        procs_to_stop.append((bot._current_recon_proc, "每日对账扫描"))

    if not procs_to_stop:
        await update.message.reply_text("当前没有正在运行的定时扫描。")
        return

    for proc, label in procs_to_stop:
        pid = proc.pid
        try:
            proc.terminate()
            try:
                await bot.asyncio.wait_for(proc.wait(), timeout=30)
                await update.message.reply_text(
                    f"🛑 已终止{label}（PID: {pid}）。\n"
                    f"锁已释放，你现在可以用 `/backfill` 了。"
                )
            except asyncio.TimeoutError:
                bot.log.warning("%s PID %s did not exit in 30s, sending SIGKILL", label, pid)
                proc.kill()
                try:
                    await proc.wait()
                except ProcessLookupError:
                    pass
                await update.message.reply_text(
                    f"🛑 {label} PID {pid} 不响应 SIGTERM，已 SIGKILL。\n"
                    f"你现在可以用 `/backfill` 了。"
                )
        except ProcessLookupError:
            await update.message.reply_text(f"{label}进程已不在了，锁应该已释放。")
        except Exception as e:
            bot.log.exception("Stop scan error: %s", e)  # noqa: TRY401
            await update.message.reply_text(f"❌ 终止失败: {e}")


async def cmd_backfill(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    await bot._show_backfill_list(update.message, update.effective_user.id, page=0)


async def show_backfill_list(bot: Any, message, telegram_user_id: int, page: int = 0) -> None:
    cfg = bot.read_config()
    users = list(reversed(bot.get_users(cfg, telegram_user_id)))
    if not users:
        await message.reply_text("📭 监控列表是空的，先 `/add` 加几个吧", parse_mode="Markdown")
        return

    markup, total_text, _page = bot.paginated_user_keyboard(
        users, page,
        item_prefix="bf",
        item_label_fmt="⏳ @{u}",
        page_prefix="bf_pg",
        close_data="bf_cl",
    )
    await message.reply_text(
        f"{total_text}\n点击用户开始全量回填：",
        reply_markup=markup,
    )


async def render_backfill_page(bot: Any, query, users: list[str], page: int) -> None:
    markup, total_text, _page = bot.paginated_user_keyboard(
        users, page,
        item_prefix="bf",
        item_label_fmt="⏳ @{u}",
        page_prefix="bf_pg",
        close_data="bf_cl",
    )
    await query.edit_message_text(
        f"{total_text}\n点击用户开始全量回填：",
        reply_markup=markup,
    )


async def cmd_backfill_callback(bot: Any, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await bot._check_auth(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    try:
        if data == "bf_cl":
            await query.edit_message_text("🔒 已关闭", reply_markup=None)
            return

        if data.startswith("bf_pg:"):
            page = int(data.split(":", 1)[1])
            cfg = bot.read_config()
            users = list(reversed(bot.get_users(cfg, uid)))
            await bot._render_backfill_page(query, users, page)
            return

        if data.startswith("bf:"):
            username = bot.resolve_callback_username(data.split(":", 1)[1])
            await query.edit_message_text(
                f"⏳ 正在全量回填 @{username}...\n这可能需要一段时间，完成后会通知你",
                reply_markup=None,
            )

            try:
                result = await bot._run_backfill(username, uid)

                if result is None:
                    await query.message.reply_text(
                        "⏱ 回填超时：连续 30 分钟无输出（idle 1800s），已终止子进程"
                    )
                    return
                if result == "busy":
                    await query.message.reply_text(
                        "❌ 无法获取回填锁，请稍后重试或输入 /stop 检查扫描状态",
                        parse_mode="Markdown",
                    )
                    return
                if result.returncode != 0:
                    stderr = result.stderr or ""
                    reason = bot._classify_failure(result.returncode, stderr)
                    bot.log.warning(
                        "Backfill failed for username=%s returncode=%s stderr=%r",
                        username, result.returncode, stderr[-4000:],
                    )
                    await query.message.reply_text(f"❌ 回填 @{username} 失败：{reason}")
                    return

                summary = bot._summarise_log(result.stderr)
                await query.message.reply_text(
                    f"✅ 回填 @{username} 完成\n{summary}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                bot.log.exception("Backfill error: %s", e)  # noqa: TRY401
                await query.message.reply_text(f"❌ 回填出错: {e}")
    except Exception as e:
        bot.log.exception("Backfill callback outer error: %s", e)  # noqa: TRY401


async def run_backfill(
    bot: Any,
    username: str,
    tg_uid: int,
) -> subprocess.CompletedProcess | None | str:
    if not bot.USERNAME_RE.fullmatch(username or ""):
        bot.log.error("Backfill rejected: invalid username %r", username)
        return None

    tg_id_str = str(tg_uid)

    cross_lock = bot._acquire_backfill_lock(tg_id_str, username)
    if cross_lock is None:
        bot.log.info("Backfill for @%s skipped: cross-process lock held", username)
        return "busy"
    cross_fd, cross_path = cross_lock

    async with bot._get_backfill_serial_lock():
        bot._register_backfill(tg_id_str, username)
        loop = bot.asyncio.get_running_loop()
        heartbeat_handle: asyncio.TimerHandle | None = None
        heartbeat_generation = 0

        def _heartbeat_tick(generation: int) -> None:
            nonlocal heartbeat_handle
            if generation != heartbeat_generation:
                bot.log.debug(
                    "Backfill heartbeat for @%s: generation %d superseded (%d is current) — stopping",
                    username, generation, heartbeat_generation,
                )
                return
            try:
                bot._register_backfill(tg_id_str, username)
            except Exception as e:
                bot.log.exception("Backfill heartbeat for @%s failed: %s", username, e)  # noqa: TRY401
            heartbeat_handle = loop.call_later(10.0, _heartbeat_tick, generation)

        heartbeat_handle = loop.call_later(10.0, _heartbeat_tick, heartbeat_generation)

        try:
            proc = await bot.asyncio.create_subprocess_exec(
                sys.executable, str(bot.SCRIPT_DIR / "backfill-memory-wrapper.py"),
                sys.executable, str(bot.MONITOR_SCRIPT), "--mode", "full", "--user", username,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(bot.SCRIPT_DIR),
                start_new_session=True,
            )
            try:
                out, err = await bot.communicate_with_idle_timeout(proc, timeout=1800)
            except asyncio.TimeoutError:
                bot.log.warning("Backfill for @%s idle timeout (30 min without output), killing", username)
                try:
                    if proc.returncode is not None:
                        bot.log.info("Backfill for @%s already exited (rc=%s), skipping killpg", username, proc.returncode)
                    else:
                        os.killpg(proc.pid, signal.SIGTERM)
                        await bot.asyncio.wait_for(proc.wait(), timeout=30)
                except (ProcessLookupError, ChildProcessError, asyncio.TimeoutError):
                    try:
                        if proc.returncode is not None:
                            bot.log.info("Backfill for @%s exited during SIGTERM wait", username)
                        else:
                            os.killpg(proc.pid, signal.SIGKILL)
                            await proc.wait()
                    except (ProcessLookupError, ChildProcessError):
                        bot.log.info("Backfill for @%s process group already gone", username)
                return None

            out_str = out.decode("utf-8") if isinstance(out, bytes) else out
            err_str = err.decode("utf-8") if isinstance(err, bytes) else err
            returncode = proc.returncode if proc.returncode is not None else -1
            return subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=out_str, stderr=err_str
            )
        finally:
            heartbeat_generation += 1
            if heartbeat_handle is not None:
                heartbeat_handle.cancel()
            bot._unregister_backfill(tg_id_str, username)
            bot._release_backfill_lock(cross_fd, cross_path)


def resume_stale_backfills(bot: Any, application: Application) -> None:
    active = bot._load_active_backfills()
    if not active:
        return

    loop = bot.asyncio.get_running_loop()
    now = bot.datetime.now(bot.timezone.utc)
    stale_threshold_minutes = 30

    bot.log.info("Found %d stale backfill tasks to resume: %s", len(active), active)

    async def _do_resume(tg_id: str, username: str, last_active_str: str | None) -> None:
        bot.log.info(
            "Auto-resuming backfill for @%s (user %s, last_active=%s)",
            username, tg_id, last_active_str,
        )

        is_zombie_by_time = False
        if last_active_str:
            try:
                last_active = bot.datetime.fromisoformat(last_active_str)
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=bot.timezone.utc)
                age_minutes = (now - last_active).total_seconds() / 60
                if age_minutes > stale_threshold_minutes:
                    is_zombie_by_time = True
                    bot.log.info("Task @%s is stale by time (%.1f min old) — will resume", username, age_minutes)
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                bot.log.warning("Could not parse last_active %s for @%s: %s", last_active_str, username, e)

        probe = bot._acquire_backfill_lock(tg_id, username)
        if probe is not None:
            fd, path = probe
            bot._release_backfill_lock(fd, path)
        lock_free = probe is not None
        if not lock_free and not is_zombie_by_time:
            bot.log.info("Skipping @%s — cross-process lock held and not stale by time (still running)", username)
            return

        bot._track_task(loop.create_task(bot._resume_backfill_task(application, tg_id, username)))

    for tg_id, users_dict in active.items():
        for username, last_active_str in users_dict.items():
            try:
                bot._track_task(loop.create_task(_do_resume(tg_id, username, last_active_str)))
            except Exception as e:
                bot.log.exception("Error resuming backfill @%s: %s", username, e)  # noqa: TRY401


async def resume_backfill_task(
    bot: Any,
    application: Application,
    tg_id: str,
    username: str,
) -> None:
    try:
        result = await bot._run_backfill(username, int(tg_id))
        if result is None:
            bot.log.warning("Resumed backfill @%s timed out", username)
            return
        if result == "busy":
            bot.log.warning("Resumed backfill @%s — monitor busy, will retry next bot start", username)
            return
        if result.returncode != 0:
            reason = bot._classify_failure(result.returncode, result.stderr or "")
            bot.log.warning("Resumed backfill @%s failed (exit %d)", username, result.returncode)
            try:
                chat_id = int(tg_id)
                await application.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ 重启后自动续接的回填 @{username} 失败：{reason}",
                )
            except Exception as e:
                bot.log.exception("Could not notify user %s: %s", tg_id, e)  # noqa: TRY401
            return

        bot.log.info("Resumed backfill @%s completed successfully", username)
        try:
            chat_id = int(tg_id)
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 后台任务完成：@{username} 的全量回填已在重启后自动续接并完成",
                parse_mode="Markdown",
            )
        except Exception as e:
            bot.log.exception("Could not notify user %s: %s", tg_id, e)  # noqa: TRY401
    except Exception as e:
        bot.log.exception("Resumed backfill @%s error: %s", username, e)  # noqa: TRY401


async def post_init(bot: Any, application: Application) -> None:
    bot._alert_bot = application.bot
    commands = [
        BotCommand("add", "增加监控对象（支持用户名/链接/@）"),
        BotCommand("remove", "取消监控对象（按钮选择）"),
        BotCommand("list", "查看当前监控列表"),
        BotCommand("status", "查看运行状态"),
        BotCommand("mode", "切换运行模式 incremental|full"),
        BotCommand("nsfw", "切换NSFW过滤 sfw_only|nsfw_only|both"),
        BotCommand("cleanup", "清理N天前的缓存图片"),
        BotCommand("scan", "立即执行一次增量扫描"),
        BotCommand("interval", "设置扫描间隔（分钟）"),
        BotCommand("stop", "终止当前定时扫描，释放锁以便回填"),
        BotCommand("backfill", "全量回填某个用户的作品"),
        BotCommand("help", "显示所有命令说明"),
    ]
    await application.bot.set_my_commands(commands)
    bot._resume_stale_backfills(application)

    def _on_cron_done(task: asyncio.Task) -> None:
        if task.cancelled():
            bot.log.info("Scheduled scan task cancelled (clean shutdown)")
            return
        exc = task.exception()
        if exc is not None:
            bot.log.error("Scheduled scan task crashed: %s", exc, exc_info=exc)

    scan_task = bot._track_task(bot.asyncio.create_task(bot.scheduled_scan_cron()))
    scan_task.add_done_callback(_on_cron_done)
    recon_task = bot._track_task(bot.asyncio.create_task(bot.scheduled_reconciliation_cron()))
    recon_task.add_done_callback(_on_cron_done)
    weekly_task = bot._track_task(bot.asyncio.create_task(bot.scheduled_weekly_report_cron()))
    weekly_task.add_done_callback(_on_cron_done)
    bot.log.info(
        f"Slash commands registered. Scheduled scan every {bot._scan_interval // 60}min. Ready."
    )


def run_bot_main(bot: Any) -> None:
    cfg = bot.read_config()
    bot._scan_interval = bot._load_interval()
    token = cfg.telegram.bot_token or os.environ.get("CIVITAI_BOT_TOKEN", "")
    if not token or token == "UNSET":
        bot.log.error("telegram.bot_token not found in config.yaml")
        sys.exit(1)

    raw_ids = cfg.authorized_users or []
    if not raw_ids:
        fallback = cfg.telegram.chat_id
        if fallback:
            try:
                bot.AUTHORIZED_USER_IDS = {int(fallback)}
            except ValueError:
                pass
    else:
        for uid in raw_ids:
            try:
                bot.AUTHORIZED_USER_IDS.add(int(uid))
            except (ValueError, TypeError):
                pass

    if not bot.AUTHORIZED_USER_IDS:
        bot.log.error("No authorized users configured (set authorized_users or telegram.chat_id)")
        sys.exit(1)

    bot.log.info("Authorised %d user(s) configured", len(bot.AUTHORIZED_USER_IDS))
    bot._ADMIN_CHAT_IDS = sorted(bot.AUTHORIZED_USER_IDS)

    api_base = cfg.telegram.api_base_url or "https://api.telegram.org"
    bot.log.info("Telegram API base: %s", api_base.rstrip("/"))
    app = (
        bot._application_builder_for_config(token, api_base)
        .post_init(bot.post_init)
        .post_shutdown(bot._shutdown_background_tasks)
        .build()
    )

    app.add_error_handler(bot._error_handler)

    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("add", bot.cmd_add))
    app.add_handler(CommandHandler("remove", bot.cmd_remove))
    app.add_handler(CommandHandler("list", bot.cmd_list))
    app.add_handler(CommandHandler("status", bot.cmd_status))
    app.add_handler(CommandHandler("mode", bot.cmd_mode))
    app.add_handler(CommandHandler("nsfw", bot.cmd_nsfw))
    app.add_handler(CommandHandler("cleanup", bot.cmd_cleanup))
    app.add_handler(CommandHandler("scan", bot.cmd_scan))
    app.add_handler(CommandHandler("stop", bot.cmd_stop))
    app.add_handler(CommandHandler("backfill", bot.cmd_backfill))
    app.add_handler(CommandHandler("interval", bot.cmd_interval))

    app.add_handler(CallbackQueryHandler(bot.cmd_remove_callback, pattern="^rem"))
    app.add_handler(CallbackQueryHandler(bot.cmd_backfill_callback, pattern="^bf"))

    bot.log.info("Civitai Admin Bot starting...")
    app.run_polling(allowed_updates=["message", "callback_query"])
