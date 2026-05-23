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

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler, ContextTypes

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
        await update.message.reply_text("Usage: `/add <username>`", parse_mode="Markdown")
        return
    username = args[1].strip()
    cfg = read_config()
    users = get_users(cfg)
    if username in users:
        await update.message.reply_text(f"👤 @{username} is already being watched.")
        return
    users.append(username)
    cfg = set_users(cfg, users)
    write_config(cfg)
    await update.message.reply_text(
        f"✅ Added @{username} to the watch list.\n"
        f"Next cron run will pick them up (every 10 min)."
    )


async def cmd_remove(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    args = update.message.text.strip().split(maxsplit=1)
    if len(args) < 2:
        await update.message.reply_text("Usage: `/remove <username>`", parse_mode="Markdown")
        return
    username = args[1].strip()
    cfg = read_config()
    users = get_users(cfg)
    if username not in users:
        await update.message.reply_text(f"👤 @{username} is not in the watch list.")
        return
    users.remove(username)
    cfg = set_users(cfg, users)
    write_config(cfg)
    await update.message.reply_text(f"✅ Removed @{username} from the watch list.")


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
        f"👥 *Users:* {len(users)} — {' '.join('@' + u for u in users) if users else 'none'}",
        f"⚙️  *Mode:* {mode} · *NSFW:* {nsfw} · *Keep:* {keep_days}d",
        f"💾 *Cache:* {download_count} images ({size_str})",
        f"📋 *Seen IDs:* {seen_count} (last update: {mtime})",
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
        await update.message.reply_text("Usage: `/backfill <username>`", parse_mode="Markdown")
        return
    username = args[1].strip()

    # Temporarily set mode to full, run, then restore
    cfg = read_config()
    orig_mode = cfg.get("mode", "incremental")
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
        cfg["mode"] = orig_mode
        # Re-read to get current user list (in case it was modified during backfill)
        current_cfg = read_config()
        current_cfg["mode"] = orig_mode
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
        cfg["mode"] = orig_mode
        write_config(cfg)
        await update.message.reply_text("⏱ Backfill timed out after 2 hours. Config restored.")
    except Exception as e:
        cfg["mode"] = orig_mode
        write_config(cfg)
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

    log.info("Civitai Admin Bot starting...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
