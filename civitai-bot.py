#!/usr/bin/env python3
"""
Civitai Monitor Admin Bot — manage your monitor via Telegram chat.

Commands:
  /add <username>       — Add a user to the watch list
  /remove <username>    — Remove a user from the watch list
  /list                 — List all watched users
  /status               — Show monitor status (disk, seen count, config)
  /mode <mode>          — Switch scan mode: incremental | full
  /nsfw <filter>        — Switch NSFW filter: sfw_only | nsfw_only | both
  /cleanup [days]       — Manually clean cached images older than N days
  /scan                 — Trigger an immediate incremental scan
  /backfill <username>  — Run a full backfill for a user
  /help                 — Show this message

Run as a systemd service for 24/7 availability.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

# Import unified config from monitor
from monitor import MonitorConfig, load_config

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("civitai-bot")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
SEEN_PATH = SCRIPT_DIR / "seen_ids.json"
DOWNLOAD_DIR = SCRIPT_DIR / "downloads"
MONITOR_SCRIPT = SCRIPT_DIR / "monitor.py"

# Authorised user — resolved from config at startup
AUTHORIZED_USER_IDS: set[int] = set()

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def read_config() -> MonitorConfig:
    if not CONFIG_PATH.exists():
        # Return minimal config so bot can still respond before setup
        return MonitorConfig(
            telegram={"bot_token": "UNSET", "chat_id": "UNSET"},
            subscriptions={},
            authorized_users=[],
        )
    # Load config safely: if config is corrupted or validation fails,
    # fall back to minimal config instead of crashing the bot process
    try:
        return load_config(CONFIG_PATH)
    except SystemExit:
        log.warning("Config validation failed, using minimal config fallback")
        return MonitorConfig(
            telegram={"bot_token": "UNSET", "chat_id": "UNSET"},
            subscriptions={},
            authorized_users=[],
        )


def write_config(cfg: MonitorConfig) -> None:
    data = cfg.model_dump(exclude_none=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True, indent=2)


def get_users(cfg: MonitorConfig, telegram_user_id: int) -> list[str]:
    subs = cfg.subscriptions or {}
    raw = subs.get(str(telegram_user_id), [])
    return [u.get("name", str(u)) if isinstance(u, dict) else str(u) for u in raw]


def set_users(cfg: MonitorConfig, telegram_user_id: int, users: list[str]) -> MonitorConfig:
    if not cfg.subscriptions:
        cfg.subscriptions = {}
    cfg.subscriptions[str(telegram_user_id)] = [{"name": u} for u in users]
    return cfg


# ---------------------------------------------------------------------------
# Username parsing & validation
# ---------------------------------------------------------------------------

CIVITAI_API = "https://civitai.com/api/v1"
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,64}$")

# Known Civitai user profile URL prefixes (only these are accepted)
_CIVITAI_URL_PREFIXES = (
    "https://www.civitai.com/user/", "http://www.civitai.com/user/",
    "https://civitai.com/user/", "http://civitai.com/user/",
    "www.civitai.com/user/", "civitai.com/user/",
    "https://www.civitai.red/user/", "http://www.civitai.red/user/",
    "https://civitai.red/user/", "http://civitai.red/user/",
    "www.civitai.red/user/", "civitai.red/user/",
)


def parse_username_input(raw: str) -> str | None:
    """Extract Civitai username from various input formats.

    Only accepts:
      - https://civitai.com/user/Username
      - https://civitai.red/user/Username
      - @Username
      - Username (plain text)

    Returns None if input doesn't match any known format.
    """
    text = raw.strip()

    # URL: only match known Civitai domains
    for prefix in _CIVITAI_URL_PREFIXES:
        if text.lower().startswith(prefix.lower()):
            cand = text[len(prefix):].split("/")[0].split("?")[0].split("#")[0]
            if USERNAME_RE.match(cand):
                return cand
            return None

    # Also reject URLs that look like a different site entirely
    if text.startswith(("http://", "https://", "www.")):
        return None

    # @username
    if text.startswith("@"):
        cand = text[1:]
        if USERNAME_RE.match(cand):
            return cand
        return None

    # Plain username
    if USERNAME_RE.match(text):
        return text

    return None


def validate_username_exists(username: str) -> tuple[bool, str]:
    """Check if a Civitai username exists and has public content.

    Since NSFW content moved to civitai.red, we query both domains
    with cookies + browsingLevel to detect all content types.
    Returns (ok, message).
    """
    # Try to load cookies for NSFW API access
    s = requests.Session()
    s.headers.update({"User-Agent": "CivitaiMonitor/2.0"})
    cookies_path = SCRIPT_DIR / "civitai_cookies.txt"
    if cookies_path.exists():
        try:
            cj = http.cookiejar.MozillaCookieJar(str(cookies_path))
            cj.load(ignore_expires=True, ignore_discard=True)
            s.cookies.update(cj)
        except Exception:
            pass

    has_sfw = False
    has_nsfw = False
    errors = []
    apis = [("civitai.com", "https://civitai.com/api/v1/images"),
            ("civitai.red", "https://civitai.red/api/v1/images")]

    for api_name, api_url in apis:
        for nsfw_flag, label in [(False, "SFW"), (True, "NSFW")]:
            if not nsfw_flag and api_name != "civitai.com":
                continue  # SFW only needed from civitai.com
            try:
                # NOTE: some users return 0 items with sort=Newest (Civitai API bug),
                # so we omit sort entirely and use the API default (Most Reactions).
                # The goal here is just to verify the user HAS public content.
                params: dict = {"username": username, "limit": 5}
                if nsfw_flag:
                    params["nsfw"] = "true"
                    params["browsingLevel"] = 8
                resp = s.get(api_url, params=params, timeout=10)
                if resp.status_code == 404:
                    return False, f"❌ 用户 @{username} 不存在（Civitai 返回 404）"
                if resp.status_code == 403:
                    return False, f"❌ 无法验证 @{username}（被 API 拒绝，可能已封禁或限制访问）"
                resp.raise_for_status()
                items = resp.json().get("items", [])
                if items:
                    if nsfw_flag:
                        has_nsfw = True
                    else:
                        has_sfw = True
            except requests.RequestException as e:
                errors.append(f"{api_name}/{label}: {e}")

    if errors:
        return False, f"❌ 验证用户时出错: {'; '.join(errors)}"

    if not has_sfw and not has_nsfw:
        return False, f"❌ 用户 @{username} 存在但未找到公开作品，无法监控"

    parts = []
    if has_sfw:
        parts.append("SFW ✅")
    if has_nsfw:
        parts.append("NSFW ✅")
    return True, f"✅ 用户 @{username} 存在，有公开作品（{' '.join(parts)}）"


# ---------------------------------------------------------------------------
# Authorisation guard
# ---------------------------------------------------------------------------


async def _check_auth(update: Update) -> bool:
    """Return True if the sender is authorised."""
    if update.effective_user and update.effective_user.id in AUTHORIZED_USER_IDS:
        return True
    if update.message:
        await update.message.reply_text("⛔ 未授权，此 Bot 仅限主人使用")
    return False


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
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


async def cmd_add(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
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

    # 智能识别用户名
    username = parse_username_input(args[1])
    if not username:
        await update.message.reply_text(
            "❌ 无法识别用户名。支持的格式：\n"
            "• 纯用户名: `TargetUser`\n"
            "• 主页链接: `https://civitai.com/user/xxx`\n"
            "• @用户名: `@TargetUser`",
            parse_mode="Markdown",
        )
        return

    # 格式校验
    if not USERNAME_RE.match(username):
        await update.message.reply_text(
            f"❌ 用户名 `{username}` 格式无效（仅允许字母、数字、下划线、连字符和点，2-64位）",
            parse_mode="Markdown",
        )
        return

    # 发送验证中提示
    verifying_msg = await update.message.reply_text(f"⏳ 正在验证 @{username} 是否存在...")

    # 存在性校验
    ok, msg = validate_username_exists(username)
    if not ok:
        await verifying_msg.edit_text(msg)
        return
    await verifying_msg.edit_text(msg)

    # 检查是否已监控
    cfg = read_config()
    uid = update.effective_user.id
    users = get_users(cfg, uid)
    if username in users:
        await update.message.reply_text(f"👤 @{username} 已经在监控列表中了")
        return

    # 添加用户
    users.append(username)
    cfg = set_users(cfg, uid, users)
    write_config(cfg)
    await update.message.reply_text(
        f"✅ 已添加 @{username} 到监控列表\n"
        f"下次定时任务（每10分钟）将自动开始抓取",
    )


async def cmd_remove(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    await _show_remove_list(update.message, update.effective_user.id, page=0)


async def _show_remove_list(message, telegram_user_id: int, page: int = 0) -> None:
    """Display paginated user list with remove buttons."""
    cfg = read_config()
    users = get_users(cfg, telegram_user_id)
    if not users:
        await message.reply_text("📭 监控列表是空的，先 `/add` 加几个吧", parse_mode="Markdown")
        return

    per_page = 8
    total_pages = (len(users) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    page_users = users[start:end]

    keyboard = []
    for u in page_users:
        keyboard.append([InlineKeyboardButton(f"❌ @{u}", callback_data=f"rem:{u}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"rem_pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"rem_pg:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔒 关闭", callback_data="rem_cl")])

    total_text = f"👥 共 {len(users)} 个监控对象" if total_pages <= 1 else f"👥 共 {len(users)} 个（第 {page + 1}/{total_pages} 页）"
    await message.reply_text(
        f"{total_text}\n点击按钮取消关注：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_remove_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle remove button presses."""
    if not await _check_auth(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    try:
        # Close — remove keyboard to avoid repeat clicks
        if data == "rem_cl":
            await query.edit_message_text("🔒 已关闭", reply_markup=None)
            return

        # Pagination
        if data.startswith("rem_pg:"):
            page = int(data.split(":", 1)[1])
            cfg = read_config()
            users = get_users(cfg, uid)
            await _render_remove_page(query, users, page)
            return

        # Remove user
        if data.startswith("rem:"):
            username = data.split(":", 1)[1]
            cfg = read_config()
            users = get_users(cfg, uid)
            if username not in users:
                await query.edit_message_text(f"👤 @{username} 已不在监控列表中", reply_markup=None)
                return
            users.remove(username)
            cfg = set_users(cfg, uid, users)
            write_config(cfg)

            if users:
                await query.edit_message_text(f"✅ 已取消关注 @{username}", reply_markup=None)
                await _show_remove_list(query.message, uid, page=0)
            else:
                await query.edit_message_text(f"✅ 已取消关注 @{username}\n📭 监控列表已清空", reply_markup=None)
    except Exception as e:
        log.error("Remove callback error: %s", e)
        try:
            await query.edit_message_text(f"❌ 操作失败，请重试 /remove", reply_markup=None)
        except Exception:
            pass


async def _render_remove_page(query, users: list[str], page: int) -> None:
    """Update the message with a fresh page of remove buttons."""
    per_page = 8
    total_pages = (len(users) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    page_users = users[start:end]

    keyboard = []
    for u in page_users:
        keyboard.append([InlineKeyboardButton(f"❌ @{u}", callback_data=f"rem:{u}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"rem_pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"rem_pg:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔒 关闭", callback_data="rem_cl")])

    total_text = f"👥 共 {len(users)} 个监控对象" if total_pages <= 1 else f"👥 共 {len(users)} 个（第 {page + 1}/{total_pages} 页）"
    await query.edit_message_text(
        f"{total_text}\n点击按钮取消关注：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_list(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    cfg = read_config()
    users = get_users(cfg, update.effective_user.id)
    if not users:
        await update.message.reply_text("📭 No users configured. Use `/add` to add some.", parse_mode="Markdown")
        return
    lines = [f"👤 {u}" for u in users]
    text = f"*Watched users ({len(lines)}):*\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    cfg = read_config()
    users = get_users(cfg, update.effective_user.id)

    # Config summary
    mode = cfg.mode
    nsfw = cfg.nsfw
    keep_days = cfg.download.keep_days
    video = cfg.video_enabled
    max_video = cfg.max_video_size_mb

    # Stats
    seen_count = 0
    mtime = ""
    data_dir = Path(cfg.data.data_dir) if cfg.data.data_dir else SCRIPT_DIR
    seen_dir = data_dir / "seen_ids"
    if seen_dir.exists():
        latest_mtime = 0
        for f in seen_dir.iterdir():
            if f.suffix == ".json":
                try:
                    data = json.loads(f.read_text())
                    seen_count += len(data) if isinstance(data, list) else 0
                    if f.stat().st_mtime > latest_mtime:
                        latest_mtime = f.stat().st_mtime
                except Exception:
                    pass
        if latest_mtime:
            from zoneinfo import ZoneInfo
            mt = datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
            bj = mt.astimezone(ZoneInfo("Asia/Shanghai"))
            mtime = bj.strftime("%Y-%m-%d %H:%M")

    download_count = 0
    download_size = 0
    if DOWNLOAD_DIR.exists():
        download_count = len(list(DOWNLOAD_DIR.iterdir()))
        download_size = sum(f.stat().st_size for f in DOWNLOAD_DIR.iterdir() if f.is_file())

    size_str = _human_size(download_size)

    lines = [
        f"👥 监控 · {len(users)} 人",
        "",
        f"💾 缓存 · {size_str}",
        "",
        f"🕐 更新 · {mtime}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_mode(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/mode <incremental|full>`", parse_mode="Markdown"
        )
        return
    mode = args[1].strip().lower()
    if mode not in ("incremental", "full"):
        await update.message.reply_text("Invalid mode. Choose `incremental` or `full`.", parse_mode="Markdown")
        return
    cfg = read_config()
    cfg.mode = mode
    write_config(cfg)
    await update.message.reply_text(f"✅ Mode set to `{mode}`.", parse_mode="Markdown")


async def cmd_nsfw(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "Usage: `/nsfw <sfw_only|nsfw_only|both>`", parse_mode="Markdown"
        )
        return
    val = args[1].strip().lower()
    if val not in ("sfw_only", "nsfw_only", "both"):
        await update.message.reply_text("Invalid. Choose `sfw_only`, `nsfw_only`, or `both`.", parse_mode="Markdown")
        return
    cfg = read_config()
    cfg.nsfw = val
    write_config(cfg)
    await update.message.reply_text(f"✅ NSFW filter set to `{val}`.", parse_mode="Markdown")


async def cmd_cleanup(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    days = 7
    if len(args) >= 2:
        try:
            days = int(args[1])
        except ValueError:
            await update.message.reply_text("Usage: `/cleanup [days]` — days must be a number.", parse_mode="Markdown")
            return

    if not DOWNLOAD_DIR.exists():
        await update.message.reply_text("📂 Download directory does not exist — nothing to clean.")
        return

    cutoff = time.time() - days * 86400
    removed = 0
    size_freed = 0
    for f in DOWNLOAD_DIR.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            size_freed += f.stat().st_size
            f.unlink()
            removed += 1

    # Also clean videos subdirectory
    video_dir = DOWNLOAD_DIR / "videos"
    if video_dir.exists():
        for f in video_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                size_freed += f.stat().st_size
                f.unlink()
                removed += 1

    if removed:
        await update.message.reply_text(
            f"🧹 Cleaned {removed} cached images older than {days} days "
            f"(freed {_human_size(size_freed)})."
        )
    else:
        await update.message.reply_text(f"📂 No cached images older than {days} days.")


async def cmd_scan(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    await update.message.reply_text("🔍 Running incremental scan... (this may take a minute)")
    try:
        result = subprocess.run(
            [sys.executable, str(MONITOR_SCRIPT)],
            capture_output=True, text=True, timeout=300, cwd=str(SCRIPT_DIR),
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode != 0:
            await update.message.reply_text(
                f"❌ Scan failed (exit {result.returncode}):\n`{stderr[-500:]}`",
                parse_mode="Markdown",
            )
            return
        # Log output is on stderr; stdout is empty in incremental mode (no JSON output)
        # Try to extract meaningful info from stderr
        summary = _summarise_log(stderr)
        await update.message.reply_text(
            f"✅ Scan complete.\n{summary}",
            parse_mode="Markdown",
        )
    except subprocess.TimeoutExpired:
        await update.message.reply_text("⏱ Scan timed out after 5 minutes.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_backfill(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    await _show_backfill_list(update.message, update.effective_user.id, page=0)


async def _show_backfill_list(message, telegram_user_id: int, page: int = 0) -> None:
    """Display paginated user list with backfill buttons."""
    cfg = read_config()
    users = get_users(cfg, telegram_user_id)
    if not users:
        await message.reply_text("📭 监控列表是空的，先 `/add` 加几个吧", parse_mode="Markdown")
        return

    per_page = 8
    total_pages = (len(users) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    page_users = users[start:end]

    keyboard = []
    for u in page_users:
        keyboard.append([InlineKeyboardButton(f"⏳ @{u}", callback_data=f"bf:{u}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"bf_pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"bf_pg:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔒 关闭", callback_data="bf_cl")])

    total_text = f"👥 共 {len(users)} 个监控对象" if total_pages <= 1 else f"👥 共 {len(users)} 个（第 {page + 1}/{total_pages} 页）"
    await message.reply_text(
        f"{total_text}\n点击用户开始全量回填：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def cmd_backfill_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle backfill button presses."""
    if not await _check_auth(update):
        return
    query = update.callback_query
    await query.answer()
    data = query.data
    uid = query.from_user.id

    try:
        # Close
        if data == "bf_cl":
            await query.edit_message_text("🔒 已关闭", reply_markup=None)
            return

        # Pagination
        if data.startswith("bf_pg:"):
            page = int(data.split(":", 1)[1])
            cfg = read_config()
            users = get_users(cfg, uid)
            await _render_backfill_page(query, users, page)
            return

        # Start backfill
        if data.startswith("bf:"):
            username = data.split(":", 1)[1]
            await query.edit_message_text(f"⏳ 正在全量回填 @{username}...\n这可能需要一段时间，完成后会通知你", reply_markup=None)

            try:
                result = subprocess.run(
                    [sys.executable, str(MONITOR_SCRIPT), "--mode", "full", "--user", username],
                    capture_output=True, text=True, timeout=7200, cwd=str(SCRIPT_DIR),
                )

                if result.returncode != 0:
                    await query.message.reply_text(f"❌ 回填 @{username} 失败（exit {result.returncode}）\n{result.stderr[:500]}")
                    return

                summary = _summarise_log(result.stderr)
                await query.message.reply_text(
                    f"✅ 回填 @{username} 完成\n{summary}",
                    parse_mode="Markdown",
                )
            except subprocess.TimeoutExpired:
                await query.message.reply_text("⏱ 回填超时（2小时）")
            except Exception as e:
                await query.message.reply_text(f"❌ 回填出错: {e}")
    except Exception as e:
        log.error("Backfill callback error: %s", e)


async def _render_backfill_page(query, users: list[str], page: int) -> None:
    """Update the message with a fresh page of backfill buttons."""
    per_page = 8
    total_pages = (len(users) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    end = start + per_page
    page_users = users[start:end]

    keyboard = []
    for u in page_users:
        keyboard.append([InlineKeyboardButton(f"⏳ @{u}", callback_data=f"bf:{u}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ 上一页", callback_data=f"bf_pg:{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("下一页 ▶", callback_data=f"bf_pg:{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔒 关闭", callback_data="bf_cl")])

    total_text = f"👥 共 {len(users)} 个监控对象" if total_pages <= 1 else f"👥 共 {len(users)} 个（第 {page + 1}/{total_pages} 页）"
    await query.edit_message_text(
        f"{total_text}\n点击用户开始全量回填：",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _human_size(bytes_: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if bytes_ < 1024:
            return f"{bytes_:.1f}{unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f}TB"


def _summarise_log(stderr: str) -> str:
    """Extract the most interesting lines from the monitor.py stderr output."""
    lines = stderr.strip().split("\n")
    # Collect relevant lines
    relevant = []
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in ("new images", "no new", "cleaned", "pushed", "new artwork", "new:", "complete", "total new", "fetched", "saved")):
            # Strip timestamp prefix for readability
            if " [" in line:
                line = line.split("] ", 1)[-1] if "] " in line else line
            relevant.append(line)

    if not relevant:
        return "No new images found."

    # Deduplicate and limit
    seen = set()
    unique = []
    for line in relevant:
        if line not in seen:
            seen.add(line)
            unique.append(line)

    return "```\n" + "\n".join(unique[-15:]) + "\n```"


# ---------------------------------------------------------------------------
# Startup — set bot commands
# ---------------------------------------------------------------------------


async def post_init(application: Application) -> None:
    commands = [
        BotCommand("add", "增加监控对象（支持用户名/链接/@）"),
        BotCommand("remove", "取消监控对象（按钮选择）"),
        BotCommand("list", "查看当前监控列表"),
        BotCommand("status", "查看运行状态"),
        BotCommand("mode", "切换运行模式 incremental|full"),
        BotCommand("nsfw", "切换NSFW过滤 sfw_only|nsfw_only|both"),
        BotCommand("cleanup", "清理N天前的缓存图片"),
        BotCommand("scan", "立即执行一次增量扫描"),
        BotCommand("backfill", "全量回填某个用户的作品"),
        BotCommand("help", "显示所有命令说明"),
    ]
    await application.bot.set_my_commands(commands)
    log.info("Slash commands registered. Ready.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    global AUTHORIZED_USER_IDS
    cfg = read_config()
    token = cfg.telegram.bot_token or os.environ.get("CIVITAI_BOT_TOKEN", "")
    if not token or token == "UNSET":
        log.error("telegram.bot_token not found in config.yaml")
        sys.exit(1)

    # Resolve authorised users from config
    raw_ids = cfg.authorized_users or []
    if not raw_ids:
        # Fallback to telegram.chat_id for backward compatibility
        fallback = cfg.telegram.chat_id
        if fallback:
            try:
                AUTHORIZED_USER_IDS = {int(fallback)}
            except ValueError:
                pass
    else:
        for uid in raw_ids:
            try:
                AUTHORIZED_USER_IDS.add(int(uid))
            except (ValueError, TypeError):
                pass

    if not AUTHORIZED_USER_IDS:
        log.error("No authorized users configured (set authorized_users or telegram.chat_id)")
        sys.exit(1)

    log.info("Authorised user IDs: %s", AUTHORIZED_USER_IDS)

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("nsfw", cmd_nsfw))
    app.add_handler(CommandHandler("cleanup", cmd_cleanup))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("backfill", cmd_backfill))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(cmd_remove_callback, pattern="^rem"))
    app.add_handler(CallbackQueryHandler(cmd_backfill_callback, pattern="^bf"))

    log.info("Civitai Admin Bot starting...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
