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
AUTHORIZED_USER_ID: int = 0

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def read_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {"users": []}
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return dict(yaml.safe_load(f) or {})


def write_config(cfg: dict[str, Any]) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False, allow_unicode=True, indent=2)


def get_users(cfg: dict[str, Any]) -> list[str]:
    raw = cfg.get("users", [])
    if isinstance(raw, list):
        return [u.get("name", str(u)) if isinstance(u, dict) else str(u) for u in raw]
    return []


def set_users(cfg: dict[str, Any], users: list[str]) -> dict[str, Any]:
    cfg["users"] = [{"name": u} for u in users]
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

    Makes two requests (SFW + NSFW) to handle both content types.
    Returns (ok, message).
    """
    has_sfw = False
    has_nsfw = False
    errors = []

    for nsfw_flag, label in [(False, "SFW"), (True, "NSFW")]:
        try:
            resp = requests.get(
                f"{CIVITAI_API}/images",
                params={"username": username, "limit": 5, "sort": "Newest",
                        "nsfw": "true" if nsfw_flag else "false"},
                headers={"User-Agent": "CivitaiMonitor/2.0"},
                timeout=10,
            )
            if resp.status_code == 404:
                return False, f"❌ 用户 @{username} 不存在（Civitai 返回 404）"
            if resp.status_code == 403:
                return False, f"❌ 无法验证 @{username}（被 API 拒绝，可能已封禁或限制访问）"
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            if items:
                if nsfw_flag:
                    has_nsfw = True
                else:
                    has_sfw = True
        except requests.RequestException as e:
            errors.append(f"{label}: {e}")

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
    if update.effective_user and update.effective_user.id == AUTHORIZED_USER_ID:
        return True
    if update.message:
        await update.message.reply_text("⛔ Unauthorised. This bot is private.")
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
            "示例: `/add UserThree`\n"
            "      `/add https://civitai.red/user/UserThree`\n"
            "      `/add @UserThree`",
            parse_mode="Markdown",
        )
        return

    # 智能识别用户名
    username = parse_username_input(args[1])
    if not username:
        await update.message.reply_text(
            "❌ 无法识别用户名。支持的格式：\n"
            "• 纯用户名: `UserThree`\n"
            "• 主页链接: `https://civitai.com/user/xxx`\n"
            "• @用户名: `@UserThree`",
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
    users = get_users(cfg)
    if username in users:
        await update.message.reply_text(f"👤 @{username} 已经在监控列表中了")
        return

    # 添加用户
    users.append(username)
    cfg = set_users(cfg, users)
    write_config(cfg)
    await update.message.reply_text(
        f"✅ 已添加 @{username} 到监控列表\n"
        f"下次定时任务（每10分钟）将自动开始抓取",
    )


async def cmd_remove(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    await _show_remove_list(update.message, page=0)


async def _show_remove_list(message, page: int = 0) -> None:
    """Display paginated user list with remove buttons."""
    cfg = read_config()
    users = get_users(cfg)
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

    # Close
    if data == "rem_cl":
        await query.edit_text("🔒 已关闭")
        return

    # Pagination
    if data.startswith("rem_pg:"):
        page = int(data.split(":", 1)[1])
        cfg = read_config()
        users = get_users(cfg)
        await _render_remove_page(query, users, page)
        return

    # Remove user
    if data.startswith("rem:"):
        username = data.split(":", 1)[1]
        cfg = read_config()
        users = get_users(cfg)
        if username not in users:
            await query.edit_text(f"👤 @{username} 已不在监控列表中")
            return
        users.remove(username)
        cfg = set_users(cfg, users)
        write_config(cfg)

        if users:
            await query.edit_text(f"✅ 已取消关注 @{username}")
            # Send updated list
            await _show_remove_list(query.message, page=0)
        else:
            await query.edit_text(f"✅ 已取消关注 @{username}\n📭 监控列表已清空")


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
    users = get_users(cfg)
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
    users = get_users(cfg)

    # Config summary
    mode = cfg.get("mode", "incremental")
    nsfw = cfg.get("nsfw", "both")
    keep_days = cfg.get("download", {}).get("keep_days", 7)
    video = cfg.get("video_enabled", True)
    max_video = cfg.get("max_video_size_mb", 1024)

    # Stats
    seen_count = 0
    if SEEN_PATH.exists():
        try:
            seen_data = json.loads(SEEN_PATH.read_text())
            seen_count = len(seen_data) if isinstance(seen_data, list) else 0
        except Exception:
            pass

    download_count = 0
    download_size = 0
    if DOWNLOAD_DIR.exists():
        download_count = len(list(DOWNLOAD_DIR.iterdir()))
        download_size = sum(f.stat().st_size for f in DOWNLOAD_DIR.iterdir() if f.is_file())

    size_str = _human_size(download_size)

    # Last seen_ids update
    mtime = ""
    if SEEN_PATH.exists():
        mt = datetime.fromtimestamp(SEEN_PATH.stat().st_mtime, tz=timezone.utc)
        mtime = mt.strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"👥 *监控用户:* {len(users)} 个 — {' '.join('@' + u for u in users) if users else '无'}",
        f"⚙️  *模式:* {mode} · *NSFW:* {nsfw} · *图片保留:* {keep_days}天",
        f"🎥 *视频:* {'开启' if video else '关闭'} · 上限 {max_video}MB",
        f"💾 *缓存:* {download_count} 张图片 ({size_str})",
        f"📋 *已处理:* {seen_count} 个ID (最近更新: {mtime})",
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
    cfg["mode"] = mode
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
    cfg["nsfw"] = val
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
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text(
            "用法: `/backfill <用户名>`\n"
            "示例: `/backfill UserThree`",
            parse_mode="Markdown",
        )
        return
    username = parse_username_input(args[1])
    if not username:
        await update.message.reply_text(
            "❌ 无法识别用户名。支持的格式：\n"
            "• 纯用户名: `UserThree`\n"
            "• 主页链接: `https://civitai.com/user/xxx`\n"
            "• @用户名: `@UserThree`",
            parse_mode="Markdown",
        )
        return

    # 验证用户存在
    ok, msg = validate_username_exists(username)
    if not ok:
        await update.message.reply_text(msg)
        return
    await update.message.reply_text(msg)

    # Temporarily set mode to full, run, then restore
    cfg = read_config()
    orig_mode = cfg.get("mode", "incremental")
    orig_users = cfg.get("users", [])
    cfg["mode"] = "full"
    cfg["users"] = [{"name": username}]
    write_config(cfg)

    await update.message.reply_text(
        f"⏳ Running full backfill for @{username}...\n"
        f"This may take a while. I'll notify you when done.",
        parse_mode="Markdown",
    )

    try:
        result = subprocess.run(
            [sys.executable, str(MONITOR_SCRIPT)],
            capture_output=True, text=True, timeout=7200, cwd=str(SCRIPT_DIR),
        )
        # Restore original config
        current_cfg = read_config()
        current_cfg["mode"] = orig_mode
        current_cfg["users"] = orig_users
        write_config(current_cfg)

        if result.returncode != 0:
            await update.message.reply_text(
                f"❌ Backfill failed (exit {result.returncode}). Config restored.",
            )
            return

        summary = _summarise_log(result.stderr)
        await update.message.reply_text(
            f"✅ Backfill for @{username} complete.\n{summary}",
            parse_mode="Markdown",
        )
    except subprocess.TimeoutExpired:
        current_cfg = read_config()
        current_cfg["mode"] = orig_mode
        current_cfg["users"] = orig_users
        write_config(current_cfg)
        await update.message.reply_text("⏱ Backfill timed out after 2 hours. Config restored.")
    except Exception as e:
        current_cfg = read_config()
        current_cfg["mode"] = orig_mode
        current_cfg["users"] = orig_users
        write_config(current_cfg)
        await update.message.reply_text(f"❌ Error: {e}. Config restored.")


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
        BotCommand("add", "Add a user to the watch list"),
        BotCommand("remove", "Remove a user from the watch list"),
        BotCommand("list", "List all watched users"),
        BotCommand("status", "Show monitor status"),
        BotCommand("mode", "Switch scan mode (incremental|full)"),
        BotCommand("nsfw", "Switch NSFW filter (sfw_only|nsfw_only|both)"),
        BotCommand("cleanup", "Clean cached images older than N days"),
        BotCommand("scan", "Trigger an immediate incremental scan"),
        BotCommand("backfill", "Run a full backfill for a user"),
        BotCommand("help", "Show all commands"),
    ]
    await application.bot.set_my_commands(commands)
    log.info("Bot commands registered. Ready.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    global AUTHORIZED_USER_ID
    cfg = read_config()
    token = cfg.get("telegram", {}).get("bot_token", "") or os.environ.get("CIVITAI_BOT_TOKEN", "")
    if not token:
        log.error("telegram.bot_token not found in config.yaml")
        sys.exit(1)

    # Resolve authorised user from telegram.chat_id
    chat_id_raw = cfg.get("telegram", {}).get("chat_id", "")
    if not chat_id_raw:
        log.error("telegram.chat_id is required (used as authorised user ID)")
        sys.exit(1)
    try:
        AUTHORIZED_USER_ID = int(chat_id_raw)
    except ValueError:
        log.error("telegram.chat_id must be a numeric user ID, got: %s", chat_id_raw)
        sys.exit(1)

    log.info("Authorised user ID: %d", AUTHORIZED_USER_ID)

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

    # Remove button callbacks
    app.add_handler(CallbackQueryHandler(cmd_remove_callback, pattern="^rem:"))

    log.info("Civitai Admin Bot starting...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
