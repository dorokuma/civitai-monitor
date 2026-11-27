"""Stateless config, username validation, PTB builder, and subprocess I/O helpers."""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import re
from pathlib import Path

import requests
from telegram.ext import Application

from monitor import MonitorConfig, load_config

log = logging.getLogger("civitai-bot")

CIVITAI_API = "https://civitai.com/api/v1"
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{2,64}$")

_CIVITAI_URL_PREFIXES = (
    "https://www.civitai.com/user/", "http://www.civitai.com/user/",
    "https://civitai.com/user/", "http://civitai.com/user/",
    "www.civitai.com/user/", "civitai.com/user/",
    "https://www.civitai.red/user/", "http://www.civitai.red/user/",
    "https://civitai.red/user/", "http://civitai.red/user/",
    "www.civitai.red/user/", "civitai.red/user/",
)


def _application_builder_for_config(token: str, api_base_url: str):
    """Build Application.builder wired to config telegram.api_base_url."""
    base = (api_base_url or "https://api.telegram.org").rstrip("/")
    builder = (
        Application.builder()
        .token(token)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(50.0)
        .get_updates_pool_timeout(30.0)
        .get_updates_write_timeout(30.0)
    )
    if base and base != "https://api.telegram.org":
        builder = (
            builder
            .base_url(f"{base}/bot")
            .base_file_url(f"{base}/file/bot")
        )
        if base.startswith(("http://127.0.0.1", "http://localhost")):
            builder = builder.local_mode(True)
    return builder


def read_config_from(config_path: Path) -> MonitorConfig:
    if not config_path.exists():
        return MonitorConfig(
            telegram={"bot_token": "UNSET", "chat_id": "UNSET"},
            subscriptions={},
            authorized_users=[],
        )
    cfg = load_config(config_path)
    if cfg is None:
        log.warning("Config validation failed, using minimal config fallback")
        return MonitorConfig(
            telegram={"bot_token": "UNSET", "chat_id": "UNSET"},
            subscriptions={},
            authorized_users=[],
        )
    return cfg


def get_users(cfg: MonitorConfig, telegram_user_id: int) -> list[str]:
    subs = cfg.subscriptions or {}
    raw = subs.get(str(telegram_user_id), [])
    return [u.get("name", str(u)) if isinstance(u, dict) else str(u) for u in raw]


def set_users(cfg: MonitorConfig, telegram_user_id: int, users: list[str]) -> MonitorConfig:
    if not cfg.subscriptions:
        cfg.subscriptions = {}
    cfg.subscriptions[str(telegram_user_id)] = [{"name": u} for u in users]
    return cfg


def parse_username_input(raw: str) -> str | None:
    """Extract Civitai username from various input formats."""
    text = raw.strip()

    for prefix in _CIVITAI_URL_PREFIXES:
        if text.lower().startswith(prefix.lower()):
            cand = text[len(prefix):].split("/")[0].split("?")[0].split("#")[0]
            if USERNAME_RE.match(cand):
                return cand
            return None

    if text.startswith(("http://", "https://", "www.")):
        return None

    if text.startswith("@"):
        cand = text[1:]
        if USERNAME_RE.match(cand):
            return cand
        return None

    if USERNAME_RE.match(text):
        return text

    return None


def validate_username_exists(
    username: str,
    cookies_path: str | Path | None = None,
    *,
    default_script_dir: Path | None = None,
) -> tuple[bool, str]:
    """Check if a Civitai username exists and has public content."""
    s = requests.Session()
    s.headers.update({"User-Agent": "CivitaiMonitor/2.0"})
    if cookies_path is None:
        base_dir = default_script_dir or Path(__file__).parent.parent.resolve()
        cookies_path = base_dir / "civitai_cookies.txt"
    else:
        cookies_path = Path(cookies_path)
    if cookies_path.exists():
        try:
            cj = http.cookiejar.MozillaCookieJar(str(cookies_path))
            cj.load(ignore_expires=True, ignore_discard=True)
            s.cookies.update(cj)
        except (OSError, http.cookiejar.LoadError, Exception) as e:  # noqa: BLE001
            log.warning("Failed to load cookies from %s: %s", cookies_path, e)

    has_sfw = False
    has_nsfw = False
    errors = []
    apis = [
        ("civitai.com", "https://civitai.com/api/v1/images"),
        ("civitai.red", "https://civitai.red/api/v1/images"),
    ]

    for api_name, api_url in apis:
        for nsfw_flag, label in [(False, "SFW"), (True, "NSFW")]:
            if not nsfw_flag and api_name != "civitai.com":
                continue
            try:
                params: dict = {"username": username, "limit": 5}
                if nsfw_flag:
                    params["nsfw"] = "true"
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


async def communicate_with_idle_timeout(
    proc: asyncio.subprocess.Process,
    timeout: float = 1800.0,
) -> tuple[bytes, bytes]:
    """Read stdout and stderr of proc in a streaming fashion with idle timeout."""
    out_chunks = []
    err_chunks = []
    queue = asyncio.Queue()

    async def read_stream(stream, stream_type):
        try:
            while True:
                data = await stream.read(4096)
                if not data:
                    break
                await queue.put((stream_type, data))
        except Exception as e:  # noqa: BLE001
            await queue.put(("error", e))
        finally:
            await queue.put((stream_type, None))

    stdout_task = asyncio.create_task(read_stream(proc.stdout, "stdout"))
    stderr_task = asyncio.create_task(read_stream(proc.stderr, "stderr"))

    finished_streams = 0
    try:
        while finished_streams < 2:
            try:
                stream_type, data = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                stdout_task.cancel()
                stderr_task.cancel()
                raise asyncio.TimeoutError()

            if stream_type == "error":
                raise data
            elif data is None:
                finished_streams += 1
            else:
                if stream_type == "stdout":
                    out_chunks.append(data)
                elif stream_type == "stderr":
                    err_chunks.append(data)
    finally:
        stdout_task.cancel()
        stderr_task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

    return b"".join(out_chunks), b"".join(err_chunks)
