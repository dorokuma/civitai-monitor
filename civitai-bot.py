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
import fcntl
import http.cookiejar
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from functools import wraps

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
    datefmt="%m-%d %H:%M:%S",
)
log = logging.getLogger("civitai-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

_user_last_call: dict[str, float] = defaultdict(float)

def rate_limit(min_interval: float = 5.0):
    """Per-user rate limiter: at least min_interval seconds between calls."""
    def decorator(func):
        @wraps(func)
        async def wrapper(update, context, *args, **kwargs):
            user_id = str(update.effective_user.id)
            now = time.time()
            elapsed = now - _user_last_call.get(user_id, 0)
            if elapsed < min_interval:
                remaining = int(min_interval - elapsed) + 1
                await update.message.reply_text(
                    f"⏳ 请等 {remaining} 秒后再试"
                )
                return
            _user_last_call[user_id] = now
            return await func(update, context, *args, **kwargs)
        return wrapper
    return decorator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "config.yaml"
DOWNLOAD_DIR = SCRIPT_DIR / "downloads"
MONITOR_SCRIPT = SCRIPT_DIR / "monitor.py"

# Scan interval config file
INTERVAL_CONFIG = SCRIPT_DIR / "interval.json"

# Active backfills state file (persists running backfill tasks across restarts)
ACTIVE_BACKFILLS = SCRIPT_DIR / "active_backfills.json"


# -----------------------------------------------------------------------
# Dynamic Memory Limit
# -----------------------------------------------------------------------

# Memory limit constants
_MEMORY_HARD_LIMIT = 2048 * 1024 * 1024  # 2048 MB (2G) hard cap in bytes


def _get_system_memory_mb() -> int:
    """Read total system memory in MB from /proc/meminfo."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    # Line format: "MemTotal:       16384084 kB"
                    parts = line.split()
                    if len(parts) >= 2:
                        kb = int(parts[1])
                        return kb // 1024  # Convert kB to MB
        return 0
    except (OSError, ValueError):
        return 0


def _compute_memory_max_mb() -> int:
    """Compute the MemoryMax for this service based on total system RAM.

    Rules:
      total <= 1GB  -> 55%
      total 1-2GB   -> 60%
      total >= 2GB  -> 65%  (capped at 1800 MB)
    """
    total_mb = _get_system_memory_mb()
    if total_mb <= 0:
        log.warning("Could not detect system memory, defaulting to 1500 MB")
        return 1500

    if total_mb <= 1024:
        pct = 0.55
    elif total_mb <= 2048:
        pct = 0.60
    else:
        pct = 0.65

    calculated = int(total_mb * pct)
    capped = min(calculated, _MEMORY_HARD_LIMIT // (1024 * 1024))
    log.info(
        "Memory policy: detected %d MB total | %.0f%% = %d MB | hard cap = %d MB | final = %d MB",
        total_mb, pct * 100, calculated, _MEMORY_HARD_LIMIT // (1024 * 1024), capped,
    )
    return capped


def _apply_memory_limit(service_name: str = "civitai-bot.service") -> None:
    """Apply MemoryMax limit to the systemd service via systemctl set-property."""
    import subprocess as _sp
    mem_mb = _compute_memory_max_mb()
    mem_bytes = mem_mb * 1024 * 1024
    try:
        result = _sp.run(
            ["systemctl", "set-property", "--now", service_name, f"MemoryMax={mem_bytes}"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            log.info("Set MemoryMax=%d MB (%d bytes) on %s", mem_mb, mem_bytes, service_name)
        else:
            log.warning("Failed to set MemoryMax: %s", result.stderr.strip())
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError, OSError) as e:
        log.warning("Could not apply MemoryMax via systemctl: %s", e)


def _load_active_backfills() -> dict[str, dict[str, str]]:
    """Load active backfills state. Returns {tg_id: {username: last_active_iso, ...}, ...}."""
    try:
        return json.loads(ACTIVE_BACKFILLS.read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        return {}


def _save_active_backfills(data: dict[str, dict[str, str]]) -> None:
    """Atomically write active backfills state via rename."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".tmp", dir=SCRIPT_DIR, delete=False, encoding="utf-8"
    )
    try:
        json.dump(data, tmp)
        tmp.close()
        os.replace(tmp.name, str(ACTIVE_BACKFILLS))
    except Exception as e:
        log.error("Failed to save active_backfills.json: %s", e)
        if os.path.exists(tmp.name):
            os.unlink(tmp.name)
        raise


# ---------------------------------------------------------------------------
# Real backfill lock — held by the bot for the entire backfill lifecycle.
# Returns the open fd; caller must call _release_backfill_lock(fd, path) to free.
# Two-tier protection:
#   1. This fd-level flock: prevents concurrent backfills across bot restarts
#      (a fresh bot sees the fd held by an orphaned process and bails out).
#   2. The asyncio.Lock (`_backfill_serial_lock`) below: serialises backfill
#      tasks within the same bot process.
# ---------------------------------------------------------------------------
def _acquire_backfill_lock(tg_id: str, username: str) -> tuple[int, Path] | None:
    """Acquire an exclusive fcntl flock for the backfill. Returns (fd, path) or None.

    The lock is held until the caller releases it via _release_backfill_lock().
    Use the .backfill_lock_*.lck file as a sentinel; remove the file on release.
    """
    lock_path = SCRIPT_DIR / f".backfill_lock_{tg_id}_{username}.lck"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, lock_path
    except (OSError, IOError):
        try:
            os.close(fd)
        except OSError:
            log.exception("Failed to close lock fd")
        return None


def _release_backfill_lock(fd: int, path: Path) -> None:
    """Release the fcntl flock and remove the lock file."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


# In-process serialisation: only one backfill runs at a time per bot process.
_backfill_serial_lock: asyncio.Lock | None = None


def _get_backfill_serial_lock() -> asyncio.Lock:
    global _backfill_serial_lock
    if _backfill_serial_lock is None:
        _backfill_serial_lock = asyncio.Lock()
    return _backfill_serial_lock


def _register_backfill(tg_id: str, username: str) -> None:
    """Register (or refresh) a running backfill so it can be resumed after restart."""
    from datetime import datetime, timezone
    data = _load_active_backfills()
    if tg_id not in data:
        data[tg_id] = {}
    data[tg_id][username] = datetime.now(timezone.utc).isoformat()
    _save_active_backfills(data)


def _unregister_backfill(tg_id: str, username: str) -> None:
    """Remove a backfill from the active registry (called on completion/failure)."""
    data = _load_active_backfills()
    if tg_id in data and username in data[tg_id]:
        del data[tg_id][username]
        if not data[tg_id]:
            del data[tg_id]
        _save_active_backfills(data)


def _load_interval() -> int:
    """Load interval from file, default 600s (10 min)."""
    try:
        return json.loads(INTERVAL_CONFIG.read_text()).get("seconds", 600)
    except (json.JSONDecodeError, OSError, FileNotFoundError) as e:
        log.warning("Failed to load interval config, using default 600s: %s", e)
        return 600


def _save_interval(seconds: int) -> None:
    INTERVAL_CONFIG.write_text(json.dumps({"seconds": seconds}))


_scan_interval: int = 600

# Track the current scheduled-scan subprocess so cmd_stop can terminate it
# without using pgrep — pgrep "python3.*monitor.py" would also match a
# running backfill subprocess and kill the wrong thing.
_current_scan_proc: asyncio.subprocess.Process | None = None

# Loop-exit flag for scheduled_scan_cron. In production this is never
# set — the scan task is torn down by PTB's Application.stop(), and the
# finally-block handles the subprocess cleanup. The flag exists for
# tests and as a documented early-exit hook.
_shutdown_requested = False

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
    cfg = load_config(CONFIG_PATH)
    if cfg is None:
        log.warning("Config validation failed, using minimal config fallback")
        return MonitorConfig(
            telegram={"bot_token": "UNSET", "chat_id": "UNSET"},
            subscriptions={},
            authorized_users=[],
        )
    return cfg


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
        except (OSError, http.cookiejar.LoadError, Exception) as e:
            log.warning("Failed to load cookies from %s: %s", cookies_path, e)

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
                    # browsingLevel omitted — cookies provide the user level
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


@rate_limit(min_interval=3.0)
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


@rate_limit(min_interval=3.0)
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


@rate_limit(min_interval=3.0)
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


@rate_limit(min_interval=3.0)
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
        log.exception("Remove callback error: %s", e)
        try:
            await query.edit_message_text("❌ 操作失败，请重试 /remove", reply_markup=None)
        except Exception as e:
            log.exception("Nested error in remove callback: %s", e)


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


@rate_limit(min_interval=3.0)
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


@rate_limit(min_interval=3.0)
async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_auth(update):
        return
    cfg = read_config()
    users = get_users(cfg, update.effective_user.id)

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
                except OSError as e:
                    log.warning("Error checking file mtime: %s", e)
        if latest_mtime:
            from zoneinfo import ZoneInfo
            mt = datetime.fromtimestamp(latest_mtime, tz=timezone.utc)
            bj = mt.astimezone(ZoneInfo("Asia/Shanghai"))
            mtime = bj.strftime("%Y-%m-%d %H:%M")

    download_size = 0
    if DOWNLOAD_DIR.exists():
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


@rate_limit(min_interval=10.0)
async def cmd_scan(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually trigger an incremental scan (mostly for debugging).

    Uses asyncio.create_subprocess_exec instead of blocking subprocess.run
    so the PTB event loop stays responsive — other Telegram messages
    (e.g. /list, /help, /backfill) are not blocked while scan runs.
    """
    if not await _check_auth(update):
        return
    await update.message.reply_text("🔍 Running incremental scan...")
    try:
        # 30-minute ceiling: incremental normally takes a few minutes, but
        # 8 users × slow Civitai API + video downloads can stretch it. The
        # scheduled cron (scheduled_scan_cron) runs without an arbitrary
        # ceiling — it just waits for the subprocess to finish on its own.
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(MONITOR_SCRIPT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SCRIPT_DIR),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except asyncio.TimeoutError:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=10)
            except (ProcessLookupError, asyncio.TimeoutError):
                proc.kill()
                await proc.wait()
            await update.message.reply_text("⏱ 扫描超时（30 分钟）已终止。")
            return
        stderr = err.decode("utf-8") if isinstance(err, bytes) else err
        if proc.returncode == 75:
            progress = _read_scan_status()
            msg = progress if progress else "⏳ 当前有定时扫描正在运行。"
            await update.message.reply_text(msg, parse_mode="Markdown")
            return
        if proc.returncode != 0:
            await update.message.reply_text(
                f"❌ Scan failed (exit {proc.returncode}):\n`{stderr[-500:]}`",
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
    except Exception as e:
        log.exception("Scan command error: %s", e)
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_interval(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Set scan interval in minutes."""
    if not await _check_auth(update):
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
        global _scan_interval
        seconds = minutes * 60
        _scan_interval = seconds
        _save_interval(seconds)
        await update.message.reply_text(f"✅ Scan interval set to {minutes} minutes.")
    except ValueError:
        await update.message.reply_text("Invalid number. Usage: `/interval <minutes>`", parse_mode="Markdown")


async def cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Stop the current scheduled scan so you can /backfill."""
    if not await _check_auth(update):
        return
    proc = _current_scan_proc
    if proc is None or proc.returncode is not None:
        await update.message.reply_text("当前没有正在运行的定时扫描。")
        return
    pid = proc.pid
    try:
        proc.terminate()  # SIGTERM — monitor.py will save state and exit cleanly
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
            await update.message.reply_text(
                f"🛑 已终止定时扫描（PID: {pid}）。\n"
                f"锁已释放，你现在可以用 `/backfill` 了。"
            )
        except asyncio.TimeoutError:
            log.warning("Scan PID %s did not exit in 30s, sending SIGKILL", pid)
            proc.kill()
            try:
                await proc.wait()
            except ProcessLookupError:
                pass
            await update.message.reply_text(
                f"🛑 扫描 PID {pid} 不响应 SIGTERM，已 SIGKILL。\n"
                f"你现在可以用 `/backfill` 了。"
            )
    except ProcessLookupError:
        await update.message.reply_text("扫描进程已不在了，锁应该已释放。")
    except Exception as e:
        log.exception("Backfill cancel error: %s", e)
        await update.message.reply_text(f"❌ 终止失败: {e}")


@rate_limit(min_interval=10.0)
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


@rate_limit(min_interval=3.0)
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
                result = await _run_backfill(username, uid)

                if result is None:
                    # Timeout
                    await query.message.reply_text("⏱ 回填超时（2小时）")
                    return
                if result == "busy":
                    # Extremely unlikely: scan still running after kill+retry
                    await query.message.reply_text(
                        "❌ 无法获取回填锁，请稍后重试或输入 /stop 检查扫描状态",
                        parse_mode="Markdown",
                    )
                    return
                if result.returncode != 0:
                    await query.message.reply_text(f"❌ 回填 @{username} 失败（exit {result.returncode}）\n{result.stderr[:500]}")
                    return

                summary = _summarise_log(result.stderr)
                await query.message.reply_text(
                    f"✅ 回填 @{username} 完成\n{summary}",
                    parse_mode="Markdown",
                )
            except Exception as e:
                log.exception("Backfill error: %s", e)
                await query.message.reply_text(f"❌ 回填出错: {e}")
    except Exception as e:
        log.exception("Backfill callback outer error: %s", e)


async def _run_backfill(username: str, tg_uid: int) -> subprocess.CompletedProcess | None | str:
    """Run backfill serially within this bot process.

    Two layers of protection:
      1. ``_backfill_serial_lock`` (asyncio.Lock): serialises concurrent backfill
         tasks within the same bot — multiple Telegram button presses queue up
         instead of racing for the .monitor.lock.
      2. ``_acquire_backfill_lock`` (fcntl.flock): cross-process lock so that a
         restarted bot can detect a backfill already in progress and not resume
         it. The lock is held for the full backfill lifetime.

    The heartbeat is now an ``asyncio`` task scheduled via ``loop.call_later``,
    bound to the lifetime of this coroutine. The ``finally`` block cancels the
    heartbeat and unregisters the backfill, regardless of how we exit (success,
    timeout, exception). This eliminates the daemon-thread leak that previously
    kept ``active_backfills.json`` populated forever after a hung task.

    Returns CompletedProcess, None (timeout), or 'busy' (cross-process lock held).
    """
    tg_id_str = str(tg_uid)

    # Layer 1: cross-process lock. If another bot process holds it, bail out
    # immediately (the original bot is still running the backfill).
    cross_lock = _acquire_backfill_lock(tg_id_str, username)
    if cross_lock is None:
        log.info("Backfill for @%s skipped: cross-process lock held", username)
        return "busy"
    cross_fd, cross_path = cross_lock

    # Layer 2: in-process serialisation. Multiple Telegram button presses for
    # the SAME user will collapse into one backfill; presses for DIFFERENT users
    # will queue behind this one.
    async with _get_backfill_serial_lock():
        # Register and start heartbeat BEFORE launching subprocess so the file
        # is populated the moment a concurrent scan tick arrives.
        _register_backfill(tg_id_str, username)
        loop = asyncio.get_running_loop()
        heartbeat_handle: asyncio.TimerHandle | None = None

        def _heartbeat_tick() -> None:
            """Refresh active_backfills.json. Schedules itself again."""
            nonlocal heartbeat_handle
            try:
                _register_backfill(tg_id_str, username)
            except Exception as e:
                log.exception("Backfill heartbeat for @%s failed: %s", username, e)
            heartbeat_handle = loop.call_later(10.0, _heartbeat_tick)

        heartbeat_handle = loop.call_later(10.0, _heartbeat_tick)

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, str(SCRIPT_DIR / "backfill-memory-wrapper.py"),
                sys.executable, str(MONITOR_SCRIPT), "--mode", "full", "--user", username,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(SCRIPT_DIR),
                start_new_session=True,
            )
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout=7200)
            except asyncio.TimeoutError:
                log.warning("Backfill for @%s exceeded 2h timeout, killing", username)
                try:
                    # Check if process already exited before killpg
                    if proc.returncode is not None:
                        log.info("Backfill for @%s already exited (rc=%s), skipping killpg", username, proc.returncode)
                    else:
                        os.killpg(proc.pid, signal.SIGTERM)
                        await asyncio.wait_for(proc.wait(), timeout=30)
                except (ProcessLookupError, ChildProcessError, asyncio.TimeoutError):
                    try:
                        if proc.returncode is not None:
                            log.info("Backfill for @%s exited during SIGTERM wait", username)
                        else:
                            os.killpg(proc.pid, signal.SIGKILL)
                            await proc.wait()
                    except (ProcessLookupError, ChildProcessError):
                        log.info("Backfill for @%s process group already gone", username)
                return None

            out_str = out.decode("utf-8") if isinstance(out, bytes) else out
            err_str = err.decode("utf-8") if isinstance(err, bytes) else err
            returncode = proc.returncode if proc.returncode is not None else -1
            return subprocess.CompletedProcess(
                args=[], returncode=returncode, stdout=out_str, stderr=err_str
            )
        finally:
            # Heartbeat is always cancelled; active_backfills is always cleared.
            # This is the only path that touches the registry for a backfill, so
            # a hung task can no longer leak state.
            if heartbeat_handle is not None:
                heartbeat_handle.cancel()
            _unregister_backfill(tg_id_str, username)
            _release_backfill_lock(cross_fd, cross_path)


def _resume_stale_backfills(application: Application) -> None:
    """Check active_backfills.json for tasks interrupted by previous shutdown and auto-resume them."""
    active = _load_active_backfills()
    if not active:
        return

    loop = asyncio.get_running_loop()

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    STALE_THRESHOLD_MINUTES = 30  # consider zombie if no heartbeat for 30 minutes

    log.info("Found %d stale backfill tasks to resume: %s", len(active), active)

    async def _do_resume(tg_id: str, username: str, last_active_str: str | None) -> None:
        log.info("Auto-resuming backfill for @%s (user %s, last_active=%s)",
                 username, tg_id, last_active_str)

        # Check timestamp as secondary zombie detection
        is_zombie_by_time = False
        if last_active_str:
            try:
                last_active = datetime.fromisoformat(last_active_str)
                # Ensure timezone-aware
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=timezone.utc)
                age_minutes = (now - last_active).total_seconds() / 60
                if age_minutes > STALE_THRESHOLD_MINUTES:
                    is_zombie_by_time = True
                    log.info("Task @%s is stale by time (%.1f min old) — will resume", username, age_minutes)
            except (KeyError, ValueError, json.JSONDecodeError) as e:
                log.warning("Could not parse last_active %s for @%s: %s", last_active_str, username, e)

        # Try to acquire the cross-process lock briefly to detect a running
        # backfill. We use timeout=0 (pure probe) — if the lock is held we know
        # another process is still working on it. The lock file is closed
        # immediately after the probe so we never accidentally hold it.
        probe = _acquire_backfill_lock(tg_id, username)
        if probe is not None:
            fd, path = probe
            _release_backfill_lock(fd, path)
        lock_free = probe is not None
        if not lock_free and not is_zombie_by_time:
            log.info("Skipping @%s — cross-process lock held and not stale by time (still running)", username)
            return

        # Schedule as a background task
        loop.create_task(_resume_backfill_task(application, tg_id, username))

    for tg_id, users_dict in active.items():
        for username, last_active_str in users_dict.items():
            try:
                loop.create_task(_do_resume(tg_id, username, last_active_str))
            except Exception as e:
                log.exception("Error resuming backfill @%s: %s", username, e)


async def _resume_backfill_task(application: Application, tg_id: str, username: str) -> None:
    """Run a resumed backfill and notify the user on completion."""
    try:
        result = await _run_backfill(username, int(tg_id))
        if result is None:
            log.warning("Resumed backfill @%s timed out", username)
            return
        if result == "busy":
            log.warning("Resumed backfill @%s — monitor busy, will retry next bot start", username)
            return
        if result.returncode != 0:
            log.warning("Resumed backfill @%s failed (exit %d)", username, result.returncode)
            return

        log.info("Resumed backfill @%s completed successfully", username)
        # Notify user if chat_id is known
        try:
            chat_id = int(tg_id)
            await application.bot.send_message(
                chat_id=chat_id,
                text=f"✅ 后台任务完成：@{username} 的全量回填已在重启后自动续接并完成",
                parse_mode="Markdown",
            )
        except Exception as e:
            log.exception("Could not notify user %s: %s", tg_id, e)
    except Exception as e:
        log.exception("Resumed backfill @%s error: %s", username, e)


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
            return f"{int(bytes_)}{unit}" if unit == "B" else f"{bytes_:.1f}{unit}"
        bytes_ /= 1024
    return f"{bytes_:.1f}TB"


def _is_scan_running() -> bool:
    """Check if monitor scan is currently running via lock file."""
    try:
        lock_path = SCRIPT_DIR / ".monitor.lock"
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


def _kill_running_scan() -> list[int]:
    """Terminate the current scheduled scan subprocess. Returns list of killed PIDs.

    Uses the tracked ``_current_scan_proc`` instead of pgrep, because pgrep on
    "python3.*monitor.py" would also match an in-flight backfill subprocess
    and kill the wrong thing. (Backfills start with ``start_new_session=True``,
    so pgrep can't distinguish them by session either.)
    """
    proc = _current_scan_proc
    if proc is None or proc.returncode is not None:
        return []
    pid = proc.pid
    try:
        proc.terminate()
        return [pid]
    except ProcessLookupError:
        return []
    except OSError as e:
        log.warning("Failed to terminate scan PID %s: %s", pid, e)
        return []


def _read_scan_status() -> str:
    """Check if monitor scan is running. Returns status text or empty string."""
    if not _is_scan_running():
        return ""
    try:
        import json
        path = SCRIPT_DIR / "monitor_status.json"
        elapsed = 0
        if path.exists():
            data = json.loads(path.read_text())
            elapsed = data.get("elapsed_seconds", 0)
        lines = ["⏳ 定时扫描运行中"]
        if elapsed >= 60:
            lines.append(f"已运行 {elapsed//60} 分钟")
        else:
            lines.append(f"已运行 {elapsed} 秒")
        lines.append("")
        lines.append("输入 /stop 终止扫描后即可 /backfill")
        return "\n".join(lines)
    except Exception:
        log.exception("Error generating status message (non-critical)")
        return ""


def _summarise_log(stderr: str) -> str:
    """Extract the most interesting lines from the monitor.py stderr output."""
    lines = stderr.strip().split("\n")
    # Collect relevant lines
    relevant = []
    for line in lines:
        lower = line.lower()
        if any(kw in lower for kw in ("new images", "no new", "cleaned", "pushed", "new artwork", "new:", "complete", "total new", "fetched", "saved", "another monitor")):
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

    safe_lines = [l.replace(chr(96), chr(92)+chr(96)) for l in unique[-15:]]
    return chr(96)*3 + chr(10) + chr(10).join(safe_lines) + chr(10) + chr(96)*3


# ---------------------------------------------------------------------------
# Startup — set bot commands
# ---------------------------------------------------------------------------


async def scheduled_scan_cron() -> None:
    """Run monitor.py on a configurable interval as a background task.

    Lifecycle:
      * Loops until PTB's Application.stop() tears the task down. The
        ``_shutdown_requested`` flag is a tests-only early-exit hook;
        in production it is never set.
      * Each iteration:
          1. Stale-watchdog: if ``active_backfills.json`` contains tasks that
             have not been refreshed in STALE_BACKFILL_MINUTES, treat them as
             zombie (e.g. the previous bot died while holding them) and
             unregister them. This is the only safety net for a bot that was
             killed -9 mid-backfill without a chance to clean up.
          2. If any active backfill remains, sleep the interval and retry.
          3. Spawn monitor.py (incremental) and wait. The .monitor.lock inside
             monitor.py is what serialises against any concurrent backfill —
             if a backfill is in progress, monitor.py exits with code 75 and we
             back off for the interval.
          4. Re-read ``interval.json`` so ``/interval`` takes effect without
             a bot restart.
      * On shutdown: SIGTERM the current monitor.py, wait up to 30s, then
        SIGKILL. We do NOT call ``os._exit`` — that would bypass PTB's own
        shutdown sequence and any in-flight backfill tasks.
    """
    cfg = read_config()
    STALE_BACKFILL_MINUTES = cfg.backfill.stale_backfill_minutes
    proc: asyncio.subprocess.Process | None = None
    try:
        while not _shutdown_requested:
            # 1. Stale watchdog — clean up zombie backfill registrations
            _sweep_stale_backfills(STALE_BACKFILL_MINUTES)

            # 2. Re-read interval at the top of every loop so /interval works
            current_interval = _load_interval()

            # 3. Skip if a non-stale backfill is still running
            active_backfills = _load_active_backfills()
            if active_backfills:
                log.info("Scheduled scan skipped: active backfill(s) %s", list(active_backfills.keys()))
                await asyncio.sleep(current_interval)
                continue
            try:
                log.info("Scheduled scan starting...")
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(MONITOR_SCRIPT),
                    cwd=str(SCRIPT_DIR),
                )
                # Publish the proc so cmd_stop can terminate it precisely,
                # without a pgrep that would also match a running backfill.
                global _current_scan_proc
                _current_scan_proc = proc
                rc = await proc.wait() if proc is not None else None
                if _shutdown_requested:
                    break
                returncode = int(rc) if rc is not None else -1
                if returncode == 0:
                    log.info("Scheduled scan completed")
                elif returncode == 75:
                    # monitor.py exits 75 when .monitor.lock is held (i.e. a
                    # backfill claimed the slot). Treat as "skipped", not a
                    # failure — back off for the rest of the interval.
                    log.info("Scheduled scan skipped: monitor lock held (backfill in progress)")
                else:
                    log.warning("Scheduled scan failed (exit %d)", returncode)
            except Exception as e:
                log.exception("Scheduled scan error: %s", e)
            if _shutdown_requested:
                break
            await asyncio.sleep(current_interval)
    finally:
        # Graceful shutdown: give the subprocess up to 30s to save and exit.
        #
        # At this point the event loop is being torn down: PTB's
        # Application.run_polling() installs its own SIGTERM handler and
        # closes the loop on shutdown, which destroys the scan task
        # mid-flight while it is inside `await proc.wait()`. asyncio's
        # `asyncio.timeout()` then raises "no running event loop" — and
        # the only reason this finally-block runs at all is because
        # Python's GC re-enters the coroutine during interpreter
        # shutdown. We catch RuntimeError and fall back to a no-wait
        # exit: the subprocess already got SIGTERM (via proc.terminate()
        # or via the process-group signal from systemd), so the OS will
        # reap it when the parent exits.
        if proc is not None and proc.returncode is None:
            log.info("Shutdown: stopping current scan...")
            try:
                proc.terminate()  # SIGTERM -> monitor.py saves current page
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=30)
                log.info("Shutdown: scan exited cleanly (exit %d)", proc.returncode)
            except RuntimeError:
                # Event loop is closed — the OS will reap the subprocess on
                # parent exit. Don't try to wait_for() in a no-loop context.
                log.info("Shutdown: event loop already closed, leaving scan to OS reaper")
            except asyncio.TimeoutError:
                log.warning("Shutdown: scan did not exit in 30s, sending SIGKILL")
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except RuntimeError:
                    pass
        log.info("Scheduled scan stopped. Returning to event loop for clean shutdown.")


def _sweep_stale_backfills(max_age_minutes: int) -> int:
    """Remove backfill registrations whose heartbeat is older than max_age_minutes.

    Returns the number of entries removed. Used by the scan cron to clean up
    after a bot that was killed mid-backfill (e.g. OOM kill, machine reboot).
    Without this, the scan cron would permanently skip forever after such an
    incident — see the post-mortem on the 8-hour deadlock in the README.
    """
    data = _load_active_backfills()
    if not data:
        return 0
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    removed = 0
    for tg_id, users_dict in list(data.items()):
        for username, last_active_str in list(users_dict.items()):
            try:
                last_active = datetime.fromisoformat(last_active_str)
                if last_active.tzinfo is None:
                    last_active = last_active.replace(tzinfo=timezone.utc)
                age_minutes = (now - last_active).total_seconds() / 60
                if age_minutes > max_age_minutes:
                    log.warning(
                        "Stale backfill detected: @%s (tg=%s) age=%.0f min — unregistering",
                        username, tg_id, age_minutes,
                    )
                    _unregister_backfill(tg_id, username)
                    removed += 1
            except Exception as e:
                log.exception("Could not parse last_active %r for @%s: %s", last_active_str, username, e)
    return removed


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
        BotCommand("interval", "设置扫描间隔（分钟）"),
        BotCommand("stop", "终止当前定时扫描，释放锁以便回填"),
        BotCommand("backfill", "全量回填某个用户的作品"),
        BotCommand("help", "显示所有命令说明"),
    ]
    await application.bot.set_my_commands(commands)
    # Auto-resume any backfills that were interrupted by a previous shutdown
    _resume_stale_backfills(application)
    # Start periodic scan as background task (PTBUserWarning is harmless).
    # Attach a done-callback so any unhandled exception inside the cron loop
    # is logged loudly instead of being swallowed by the event loop.
    def _on_cron_done(task: asyncio.Task) -> None:
        if task.cancelled():
            log.info("Scheduled scan task cancelled (clean shutdown)")
            return
        exc = task.exception()
        if exc is not None:
            log.error("Scheduled scan task crashed: %s", exc, exc_info=exc)

    scan_task = asyncio.create_task(scheduled_scan_cron())
    scan_task.add_done_callback(_on_cron_done)
    log.info(f"Slash commands registered. Scheduled scan every {_scan_interval//60}min. Ready.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    global AUTHORIZED_USER_IDS

    # Apply dynamic memory limit before anything else
    _apply_memory_limit()

    cfg = read_config()
    global _scan_interval
    _scan_interval = _load_interval()
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

    log.info("Authorised %d user(s) configured", len(AUTHORIZED_USER_IDS))

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
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("backfill", cmd_backfill))
    app.add_handler(CommandHandler("interval", cmd_interval))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(cmd_remove_callback, pattern="^rem"))
    app.add_handler(CallbackQueryHandler(cmd_backfill_callback, pattern="^bf"))

    log.info("Civitai Admin Bot starting...")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
