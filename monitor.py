#!/usr/bin/env python3

"""
Civitai Monitor — Enhanced Edition v2.0

Monitors specified Civitai users for new image & video uploads,
downloads full-resolution originals, and pushes them to a Telegram
channel via the Bot API.

Features:
  - Images + Videos (streaming download, size-limited)
  - Two modes: incremental (cron-friendly) / full (backfill)
  - NSFW filter: sfw_only / nsfw_only / both
  - Pydantic config validation
  - Tenacity retry (exponential back-off)
  - FileLock for crash-safe seen_ids persistence
  - Auto-cleanup of old cached files
  - Admin Bot companion (civitai-bot.py)

Usage:
  python3 monitor.py                          # uses config.yaml
  python3 monitor.py --config /path/to.yaml   # custom config path
  python3 monitor.py --mode full --user xxx   # backfill single user
"""

from __future__ import annotations

import load_env  # noqa: F401  # 自动加载同目录下的 civitai-bot.env（token 等敏感信息）


import argparse
import atexit
import datetime as _dt
import fcntl
import json
import logging
import re
import os
import signal
import sys
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Literal

import requests
import yaml
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import random

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%m-%d %H:%M:%S",
)
log = logging.getLogger("civitai-monitor")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------


class HttpConfig(BaseModel):
    user_agent: str = Field(
        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    referer: str = Field(default="https://civitai.com")
    extra_headers: dict[str, str] = Field(default_factory=dict)
    cookies_file: str = Field(default="", description="Path to Netscape-format cookies.txt for Civitai auth")


class DownloadConfig(BaseModel):
    output_dir: str = "downloads"
    keep_days: int = 7
    max_total_gb: int = 10          # 新增：最大缓存总大小（GB），0 表示不限制
    size_suffixes: list[str] = Field(
        default=["/width=1024/", "/width=450/", "/width=640/"]
    )


class ApiConfig(BaseModel):
    base_url: str = "https://civitai.com/api/v1"
    images_per_page: int = 100


class TelegramConfig(BaseModel):
    bot_token: str
    chat_id: str
    api_base_url: str = "https://api.telegram.org"


class DataConfig(BaseModel):
    data_dir: str = ""
    seen_ids_file: str = "seen_ids.json"


class IncrementalConfig(BaseModel):
    """Limits that apply to incremental mode (the cron-scheduled scan).

    Incremental is designed to surface *new* content, not to walk an entire
    creator's history. ``max_pages`` caps how many pages we fetch per track
    (SFW/NSFW) on any given scan, so a creator with 1k+ items does not push
    the whole history on the very first run. To backfill the full history,
    use the ``/backfill <user>`` command (which runs ``--mode full``).
    """
    max_pages: int = 5



class BackfillConfig(BaseModel):
    """Configuration for full backfill mode."""
    stale_backfill_minutes: int = Field(
        default=120,
        description="Minutes after which an active backfill is considered stale/zombie"
    )


class MonitorConfig(BaseModel):
    users: list = Field(default_factory=list)
    subscriptions: dict[str, list] = Field(default_factory=dict)
    authorized_users: list[int] = Field(default_factory=list)
    mode: Literal["incremental", "full"] = "incremental"
    nsfw: Literal["sfw_only", "nsfw_only", "both"] = "both"
    api: ApiConfig = Field(default_factory=ApiConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    telegram: TelegramConfig
    data: DataConfig = Field(default_factory=DataConfig)
    incremental: IncrementalConfig = Field(default_factory=IncrementalConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    backfill: BackfillConfig = Field(default_factory=BackfillConfig)
    video_enabled: bool = True
    max_video_size_mb: int = 1024


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path.home() / ".civitai-monitor" / "config.yaml",
    Path(__file__).parent.resolve() / "config.yaml",
]

SCRIPT_DIR = Path(__file__).parent.resolve()

VALID_NSFW = {"sfw_only", "nsfw_only", "both"}
VALID_MODES = {"incremental", "full"}

# Page-size safety: Civitai's /images endpoint silently caps or rejects
# out-of-range `limit` values, so we clamp before sending. 200 is the highest
# value the API reliably accepts.
MAX_API_PAGE_LIMIT = 200
MIN_API_PAGE_LIMIT = 1

# Telegram send limits
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
TELEGRAM_DOCUMENT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB; larger is rejected
TELEGRAM_VIDEO_MAX_MB = 2048  # Telegram sendDocument limit; sendVideo only for small previews

# Per-item timeouts (seconds) for downloads
IMAGE_DOWNLOAD_TIMEOUT = 120
VIDEO_DOWNLOAD_TIMEOUT = 120
HTTP_REQUEST_TIMEOUT = 30

# Filesystem layout sentinel
DATA_DIR_NAME = "seen_ids"  # directory holding per-user ID files
STATUS_FILE_NAME = "monitor_status.json"
LOCK_FILE_NAME = ".monitor.lock"

# ---------------------------------------------------------------------------
# Global HTTP session (enforces Referer + User-Agent on every request)
# ---------------------------------------------------------------------------

session = requests.Session()
session.timeout = 30
session.verify = True


def init_session(http_cfg: HttpConfig) -> None:
    """Apply the user-configured headers to the global session."""
    session.headers.update({
        "User-Agent": http_cfg.user_agent,
        "Referer": http_cfg.referer,
        "Accept": "*/*",
    })
    if http_cfg.extra_headers:
        session.headers.update(http_cfg.extra_headers)

    # Load Civitai cookies (needed for video CDN and NSFW API auth)
    if http_cfg.cookies_file:
        cookies_path = Path(http_cfg.cookies_file)
        if cookies_path.exists():
            import http.cookiejar
            cj = http.cookiejar.MozillaCookieJar(str(cookies_path))
            cj.load(ignore_expires=True, ignore_discard=True)
            session.cookies.update(cj)
            log.info("Loaded %d cookies from %s", len(cj), cookies_path)


# ---------------------------------------------------------------------------
# Rate-limit error (for 429 Retry-After handling)
# ---------------------------------------------------------------------------


class RateLimitError(requests.RequestException):
    """Raised when the API returns 429 Too Many Requests."""
    def __init__(self, response: requests.Response) -> None:
        self.retry_after = int(response.headers.get("Retry-After", 30))
        super().__init__(f"429 Rate Limited, retry after {self.retry_after}s")


# ---------------------------------------------------------------------------
# Tenacity-retried GET
# ---------------------------------------------------------------------------


def _rate_limit_wait(retry_state) -> float:
    """Respect Retry-After header when rate-limited, fall back to exponential backoff."""
    exc = retry_state.outcome.exception()
    if isinstance(exc, RateLimitError):
        return exc.retry_after + random.uniform(0, 2)
    return wait_exponential(multiplier=1, min=2, max=30)(retry_state)


@retry(
    stop=stop_after_attempt(5),
    wait=_rate_limit_wait,
    retry=retry_if_exception_type((requests.RequestException, RateLimitError)),
    reraise=True,
)
def safe_get(url: str, **kwargs) -> requests.Response:
    resp = session.get(url, timeout=kwargs.pop("timeout", 30), **kwargs)
    if resp.status_code == 429:
        raise RateLimitError(resp)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> MonitorConfig | None:
    paths = [Path(path)] if path else DEFAULT_CONFIG_PATHS
    for p in paths:
        if p.exists():
            log.info("Loading config from %s", p)
            with open(p, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            # Environment variable overrides for sensitive fields
            if os.environ.get("CIVITAI_BOT_TOKEN"):
                raw.setdefault("telegram", {})["bot_token"] = os.environ["CIVITAI_BOT_TOKEN"]
            try:
                cfg = MonitorConfig(**raw)
                init_session(cfg.http)
                return cfg
            except ValidationError as e:
                log.error("Config validation failed: %s", e)
                return None
    log.error("config.yaml not found (searched: %s)", [str(p) for p in paths])
    return None


# ---------------------------------------------------------------------------
# NSFW track helpers
# ---------------------------------------------------------------------------


def nsfw_tracks(nsfw_setting: str) -> list[bool | None]:
    mapping: dict[str, list[bool | None]] = {
        "sfw_only": [False],
        "nsfw_only": [True],
        "both": [False, True],
    }
    if nsfw_setting not in mapping:
        log.warning("Unknown nsfw setting %r, defaulting to 'both'", nsfw_setting)
    return mapping.get(nsfw_setting, [False, True])  # default both


# ---------------------------------------------------------------------------
# Civitai API
# ---------------------------------------------------------------------------


def fetch_page(
    username: str,
    base_url: str = "https://civitai.com/api/v1",
    limit: int = 100,
    cursor: str = "",
    nsfw: bool | None = None,
    sort: str | None = "Newest",
) -> tuple[list[dict[str, Any]], str]:
    """Fetch one page of images for a user.

    Uses cursor-based pagination (Civitai API page parameter is broken).
    Returns (items, next_cursor) where next_cursor is empty when there are no more pages.

    Args:
        cursor: Pagination cursor from previous response metadata.nextCursor.
                Empty string for the first page.
        nsfw: None → API default | False → SFW | True → NSFW
        sort: Sort order. Default "Newest". Pass None for API default (Most Reactions).
              Falls back to None automatically when "Newest" returns 0 items
              (some users have a Civitai API bug where Newest sort returns empty).
    """
    # Clamp `limit` to the Civitai-allowed range. The API silently caps at
    # 100 for some endpoints and rejects out-of-range values for others, so
    # we always coerce to a safe [MIN_API_PAGE_LIMIT, MAX_API_PAGE_LIMIT] window
    # before sending.
    limit = max(MIN_API_PAGE_LIMIT, min(int(limit), MAX_API_PAGE_LIMIT))
    params: dict[str, Any] = {
        "username": username,
        "limit": limit,
    }
    if cursor:
        params["cursor"] = cursor
    else:
        params["page"] = 1
    if sort is not None:
        params["sort"] = sort
    if nsfw is not None:
        params["nsfw"] = "true" if nsfw else "false"
    # NSFW content requires civitai.red + browsingLevel + cookies
    if nsfw is True:
        # browsingLevel omitted — cookies provide the user level
        actual_base = "https://civitai.red/api/v1"
    else:
        actual_base = base_url

    try:
        resp = safe_get(f"{actual_base}/images", params=params)
        resp.raise_for_status()
        items = resp.json().get("items", [])

        # Fallback: if Newest sort returns empty but user exists,
        # retry with default sort (Civitai API bug workaround).
        if not items and sort == "Newest":
            log.warning(
                "%s: sort=Newest returned 0 items (nsfw=%s), retrying with default sort",
                username, nsfw,
            )
            fallback_params = dict(params)
            fallback_params.pop("sort", None)
            fallback_resp = safe_get(f"{actual_base}/images", params=fallback_params)
            fallback_resp.raise_for_status()
            items = fallback_resp.json().get("items", [])
            resp = fallback_resp  # use fallback response for cursor metadata

        next_cursor = resp.json().get("metadata", {}).get("nextCursor", "") if items else ""
        return items, next_cursor
    except requests.RequestException as e:
        log.warning("Page query failed (nsfw=%s): %s", nsfw, e)
        return [], ""


# ---------------------------------------------------------------------------
# Image URL processing
# ---------------------------------------------------------------------------


def normalize_to_original(
    image_url: str,
    size_suffixes: list[str] | None = None,
) -> str:
    if size_suffixes is None:
        size_suffixes = ["/width=1024/", "/width=450/", "/width=640/"]
    for suffix in size_suffixes:
        if suffix in image_url:
            return image_url.replace(suffix, "/width=original/")
    return image_url


# ---------------------------------------------------------------------------
# seen_ids persistence (with FileLock)
# ---------------------------------------------------------------------------


LOCK_PATH = SCRIPT_DIR / LOCK_FILE_NAME
STATUS_PATH = SCRIPT_DIR / STATUS_FILE_NAME


def seen_file_for_user(seen_dir: Path, tg_id: str, username: str) -> Path:
    """Get the per-user seen IDs file path.

    Each (Telegram user, Civitai user) pair has its own independent file
    so that different Telegram accounts have separate download progress.
    """
    safe_username = re.sub(r"[^a-zA-Z0-9]", "_", username)
    seen_dir.mkdir(parents=True, exist_ok=True)
    return seen_dir / f"seen_ids_{tg_id}_{safe_username}.json"


def load_seen_ids(seen_dir: Path, tg_id: str, username: str) -> set[int]:
    """Load seen IDs for a specific (Telegram user, Civitai user) pair."""
    path = seen_file_for_user(seen_dir, tg_id, username)
    return set(json.loads(path.read_text())) if path.exists() else set()


def _save_lock_path(seen_dir: Path, name: str = "save") -> Path:
    """Per-seen_dir lock file. Keeping the lock inside the data directory
    means the global bot lock doesn't serialize unrelated writes; only
    writers targeting the same seen_dir (which is the only case that can
    actually race) contend."""
    seen_dir.mkdir(parents=True, exist_ok=True)
    return seen_dir / f".{name}.lock"


def save_seen_ids(seen_dir: Path, tg_id: str, username: str, ids: set[int]) -> None:
    """Save seen IDs for a specific (Telegram user, Civitai user) pair."""
    path = seen_file_for_user(seen_dir, tg_id, username)
    lock_path = _save_lock_path(seen_dir, name="seen")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                # Atomic write: temp file + rename to prevent corruption on crash
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(ids), indent=2))
                tmp.rename(path)
                path.chmod(0o600)
            log.info("Saved %d seen IDs for @%s", len(ids), username)
            break
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning("Timeout saving %d seen IDs for @%s, skipped after 3 attempts", len(ids), username)


def pushed_file_for_user(pushed_dir: Path, tg_id: str, username: str) -> Path:
    safe_username = re.sub(r"[^a-zA-Z0-9]", "_", username)
    pushed_dir.mkdir(parents=True, exist_ok=True)
    return pushed_dir / f"pushed_ids_{tg_id}_{safe_username}.json"


def load_pushed_ids(pushed_dir: Path, tg_id: str, username: str) -> set[int]:
    path = pushed_file_for_user(pushed_dir, tg_id, username)
    return set(json.loads(path.read_text())) if path.exists() else set()


def save_pushed_ids(pushed_dir: Path, tg_id: str, username: str, ids: set[int]) -> None:
    """Persist pushed IDs, merging with any on-disk set under the lock.

    Merge-on-write avoids clobbering IDs saved by an earlier crash-recovery
    path or a concurrent writer. The caller's ``ids`` set is updated in place
    to the merged result so in-memory state stays consistent with disk.
    """
    path = pushed_file_for_user(pushed_dir, tg_id, username)
    lock_path = _save_lock_path(pushed_dir, name="pushed")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                on_disk: set[int] = set()
                if path.exists():
                    try:
                        on_disk = set(json.loads(path.read_text()))
                    except (json.JSONDecodeError, OSError, TypeError, ValueError):
                        log.warning(
                            "Corrupt pushed IDs file for @%s, rewriting from memory",
                            username,
                        )
                merged = on_disk | set(ids)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(merged), indent=2))
                tmp.rename(path)
                path.chmod(0o600)
                # Keep caller set in sync with the merged disk view.
                ids.clear()
                ids.update(merged)
            log.info("Saved %d pushed IDs for @%s", len(merged), username)
            break
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning("Timeout saving %d pushed IDs for @%s, skipped after 3 attempts", len(ids), username)


# ---------------------------------------------------------------------------
# Push lifecycle state: inflight (pre-claim) + pending (timeout / uncertain)
# ---------------------------------------------------------------------------
#
# Timeline for one item:
#   1. mark inflight  →  2. send to Telegram  →  3a. OK: pushed + clear inflight
#                                              →  3b. timeout: pending + clear inflight
#                                              →  3c. fail: clear inflight only
#                                              →  3d. crash mid-send: inflight left on disk
# Next scan:
#   - leftover inflight is promoted to pending (treat as uncertain)
#   - pending younger than PENDING_CONFIRM_SECONDS → assume delivered, promote to pushed
#   - pending older → one retry push
#
# Telegram has no cheap "did bot message X land?" check for outbound media, so the
# "light confirm" is age-based: fresh uncertain stays non-retrying; stale retries once.

PENDING_CONFIRM_SECONDS = 30 * 60  # 30 minutes
# After one expired retry that is still uncertain, promote without further re-sends
# (caps the "maybe already delivered" loop at a single extra push attempt).
PENDING_MAX_RETRIES = 1


def _safe_user_token(username: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", username)


def _push_state_file(state_dir: Path, kind: str, tg_id: str, username: str) -> Path:
    """kind is 'pending' or 'inflight'."""
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{kind}_push_{tg_id}_{_safe_user_token(username)}.json"


def load_push_timestamps(state_dir: Path, kind: str, tg_id: str, username: str) -> dict[int, float]:
    """Load id → unix-ts map for inflight (simple floats). Corrupt/missing → empty."""
    path = _push_state_file(state_dir, kind, tg_id, username)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {}
        out: dict[int, float] = {}
        for k, v in raw.items():
            try:
                # Allow legacy pending-style dicts if misread as inflight.
                if isinstance(v, dict):
                    out[int(k)] = float(v.get("ts", 0))
                else:
                    out[int(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        log.warning("Corrupt %s push state for @%s, starting empty", kind, username)
        return {}


def _write_push_timestamps(path: Path, data: dict[int, float]) -> None:
    """Atomic rewrite of an id→ts map (caller must hold the lock)."""
    serializable = {str(k): v for k, v in sorted(data.items())}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serializable, indent=2))
    tmp.rename(path)
    path.chmod(0o600)


def update_push_timestamps(
    state_dir: Path,
    kind: str,
    tg_id: str,
    username: str,
    *,
    add: dict[int, float] | None = None,
    remove: set[int] | None = None,
) -> dict[int, float]:
    """Merge add / remove into an inflight-style float map under a file lock."""
    path = _push_state_file(state_dir, kind, tg_id, username)
    lock_path = _save_lock_path(state_dir, name=f"{kind}_push")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                data = load_push_timestamps(state_dir, kind, tg_id, username)
                if add:
                    data.update(add)
                if remove:
                    for iid in remove:
                        data.pop(iid, None)
                if data:
                    _write_push_timestamps(path, data)
                elif path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        _write_push_timestamps(path, {})
                return data
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning(
                    "Timeout updating %s push state for @%s after 3 attempts",
                    kind, username,
                )
    return load_push_timestamps(state_dir, kind, tg_id, username)


def mark_inflight(state_dir: Path, tg_id: str, username: str, item_id: int) -> None:
    """Pre-claim an item ID before the Telegram request leaves the process."""
    update_push_timestamps(
        state_dir, "inflight", tg_id, username,
        add={item_id: time.time()},
    )


def clear_inflight(state_dir: Path, tg_id: str, username: str, item_id: int) -> None:
    update_push_timestamps(
        state_dir, "inflight", tg_id, username,
        remove={item_id},
    )


# Pending records: id → (ts, retries). Legacy on-disk value may be a bare float.
PendingMap = dict[int, tuple[float, int]]


def _parse_pending_value(v: Any) -> tuple[float, int] | None:
    try:
        if isinstance(v, dict):
            return float(v.get("ts", 0)), int(v.get("retries", 0))
        return float(v), 0
    except (TypeError, ValueError):
        return None


def load_pending_map(state_dir: Path, tg_id: str, username: str) -> PendingMap:
    """Load pending id → (ts, retries). Supports legacy float-only values."""
    path = _push_state_file(state_dir, "pending", tg_id, username)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {}
        out: PendingMap = {}
        for k, v in raw.items():
            parsed = _parse_pending_value(v)
            if parsed is None:
                continue
            try:
                out[int(k)] = parsed
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        log.warning("Corrupt pending push state for @%s, starting empty", username)
        return {}


def _write_pending_map(path: Path, data: PendingMap) -> None:
    serializable = {
        str(k): {"ts": ts, "retries": retries}
        for k, (ts, retries) in sorted(data.items())
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serializable, indent=2))
    tmp.rename(path)
    path.chmod(0o600)


def update_pending_map(
    state_dir: Path,
    tg_id: str,
    username: str,
    *,
    add: PendingMap | None = None,
    remove: set[int] | None = None,
) -> PendingMap:
    """Merge add/remove into the pending map under lock; atomic rewrite."""
    path = _push_state_file(state_dir, "pending", tg_id, username)
    lock_path = _save_lock_path(state_dir, name="pending_push")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                data = load_pending_map(state_dir, tg_id, username)
                if add:
                    data.update(add)
                if remove:
                    for iid in remove:
                        data.pop(iid, None)
                if data:
                    _write_pending_map(path, data)
                elif path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        _write_pending_map(path, {})
                return data
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning(
                    "Timeout updating pending push state for @%s after 3 attempts",
                    username,
                )
    return load_pending_map(state_dir, tg_id, username)


def mark_pending(
    state_dir: Path,
    tg_id: str,
    username: str,
    item_id: int,
    *,
    ts: float | None = None,
    retries: int = 0,
) -> None:
    """Record uncertain delivery (timeout or leftover inflight after crash)."""
    update_pending_map(
        state_dir, tg_id, username,
        add={item_id: (ts if ts is not None else time.time(), int(retries))},
    )


def clear_pending(state_dir: Path, tg_id: str, username: str, item_id: int) -> None:
    update_pending_map(state_dir, tg_id, username, remove={item_id})


def adopt_stale_inflight(state_dir: Path, tg_id: str, username: str) -> PendingMap:
    """Move any leftover inflight IDs into pending (crash mid-send recovery).

    Returns the pending map after adoption.
    """
    inflight = load_push_timestamps(state_dir, "inflight", tg_id, username)
    if not inflight:
        return load_pending_map(state_dir, tg_id, username)
    log.warning(
        "Adopting %d leftover inflight ID(s) as pending for @%s (crash recovery)",
        len(inflight), username,
    )
    update_pending_map(
        state_dir, tg_id, username,
        add={iid: (ts, 0) for iid, ts in inflight.items()},
    )
    update_push_timestamps(state_dir, "inflight", tg_id, username, remove=set(inflight.keys()))
    return load_pending_map(state_dir, tg_id, username)


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------


def download_image(url: str, save_path: Path, timeout: int = 120) -> bool:
    if save_path.exists():
        log.info("Already exists: %s, skipped", save_path.name)
        return True
    try:
        resp = safe_get(url, stream=True, timeout=timeout)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
        tmp_path.rename(save_path)
        log.info("Downloaded: %s (%d bytes)", save_path.name, save_path.stat().st_size)
        return True
    except requests.RequestException as e:
        log.warning("Image download failed for %s: %s", url, e)
        return False


# ---------------------------------------------------------------------------
# Video download (streaming + size check)
# ---------------------------------------------------------------------------


def download_video(url: str, save_path: Path, max_size_mb: int = 1024) -> bool:
    if save_path.exists():
        log.info("Already exists: %s, skipped", save_path.name)
        return True
    """Download a full-quality video from Civitai CDN.

    Strategy:
      1. Follow redirect from image.civitai.com → B2 /default (cover)
      2. Rewrite /default → /original on B2 to get the real video
      3. If /original is 404 (video no longer available), skip

    ``max_size_mb`` semantics:
      * ``> 0``  — skip videos whose Content-Length exceeds the cap.
      * ``<= 0`` — disable the size cap (download every video regardless of
                    size). The config example shows ``max_video_size_mb:
                    1024``; set to 0 to opt out of the cap entirely.
    """
    try:
        # Step 1: follow redirect to find the real CDN URL (don't download body)
        resp = safe_get(url, stream=True)
        resp.raise_for_status()
        b2_url = str(resp.url)
        resp.close()

        # Step 2: resolve the actual video content URL
        if "image-b2.civitai.com" in b2_url and b2_url.endswith("/default"):
            resolved_url = b2_url[:-8] + "/original"
            log.info("B2: /default → /original")
            # Retry up to 3 times on transient errors, but not on 404
            for attempt in range(3):
                resp = safe_get(resolved_url, stream=True, timeout=120)
                if resp.status_code == 200:
                    break
                if resp.status_code == 404:
                    log.warning("B2 /original 404 — video file not available on CDN")
                    return False
                if attempt < 2:
                    log.info("B2 /original returned %d, retrying (%d/3)...", resp.status_code, attempt + 2)
                    time.sleep(2 ** attempt)
            resp.raise_for_status()
        else:
            # Non-B2 CDN (e.g. civitai.red): re-fetch original URL with fresh connection
            log.info("Non-B2 video CDN: %s...", b2_url[:80])
            resp = safe_get(url, stream=True, timeout=120)
            resp.raise_for_status()

        # Check size — skip only when a positive cap is configured
        cl = resp.headers.get("content-length")
        if max_size_mb > 0 and cl and int(cl) > max_size_mb * 1024 * 1024:
            log.warning("Video too large (%.1f MB > %d MB), skipping",
                        int(cl) / 1024 / 1024, max_size_mb)
            return False
        if not cl and max_size_mb > 0:
            log.info("Video size unknown (no Content-Length), downloading anyway up to %d MB cap", max_size_mb)

        save_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        tmp_path.rename(save_path)
        log.info("Video downloaded: %s (%.1f MB)", save_path.name, save_path.stat().st_size / 1024 / 1024)
        return True
    except requests.RequestException as e:
        log.warning("Video download failed for %s: %s", url, e)
        return False


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Telegram push
# ---------------------------------------------------------------------------


_tg_api_base = "https://api.telegram.org"

def send_to_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None = None,
) -> bool | None:
    """Send a Telegram message.

    Returns:
      True  — confirmed delivery
      False — confirmed failure (safe to retry)
      None  — uncertain (timeout after request left; may already be delivered)
    """
    api_base = f"{_tg_api_base}/bot{bot_token}"

    if file_paths:
        valid_files = [fp for fp in file_paths if fp.exists()]
        if valid_files:
            # Determine media type from file extension
            is_video = any(fp.suffix.lower() in (".mp4", ".webm", ".mov") for fp in valid_files)

            if is_video:
                return _send_telegram_video(api_base, chat_id, text, valid_files[0])

            # Images — try media group first, fall back to single
            return _send_telegram_media_group(api_base, chat_id, text, valid_files)

    # Text-only fallback
    return _send_telegram_text(api_base, chat_id, text)


def _send_telegram_video(api_base: str, chat_id: str, text: str, video_path: Path) -> bool | None:
    """Send a video to Telegram. <=50 MB: sendVideo (inline play). >50 MB: sendDocument (2 GB, no quality loss).

    Delivery policy (anti-duplicate):
      * HTTP success → True
      * Clear HTTP error → text-only fallback (media was rejected; safe to notify)
      * Timeout after the request left the client → None (uncertain; no text fallback)
      * Other transport errors → False without text (retry media next scan)
    """
    size_mb = video_path.stat().st_size / 1048576
    try:
        if size_mb <= TELEGRAM_VIDEO_MAX_MB:
            endpoint = f"{api_base}/sendVideo"
            field_name = "video"
        else:
            endpoint = f"{api_base}/sendDocument"
            field_name = "document"
            log.info("Video %.1f MB > %d MB, sending as document (2 GB limit)", size_mb, TELEGRAM_VIDEO_MAX_MB)
        with open(video_path, "rb") as f:
            resp = requests.post(
                endpoint,
                data={"chat_id": chat_id, "caption": text, "parse_mode": "Markdown"},
                files={field_name: (video_path.name, f, "video/mp4")},
                timeout=300,
            )
        if resp.ok:
            return True
        log.warning("Video send failed: %s", resp.text[:200])
        # Media rejected by API — text notify is safe (no prior delivery).
        return _send_telegram_text(api_base, chat_id, text)
    except requests.Timeout as e:
        log.warning(
            "Video send timeout (may already be delivered); marking uncertain, no text fallback: %s",
            e,
        )
        return None
    except requests.RequestException as e:
        log.warning("Video send error (will retry later, no text fallback): %s", e)
        return False


def _send_telegram_media_group(api_base: str, chat_id: str, text: str, file_paths: list[Path]) -> bool | None:
    """Send images as a media group.

    Same anti-duplicate policy as ``_send_telegram_video``: never append a
    text message after a transport timeout (the photo may already be in the chat).
    Timeout returns None (uncertain) so the caller can park the ID as pending.
    """
    media = []
    files: dict[str, tuple] = {}
    open_handles: list = []
    try:
        for i, fp in enumerate(file_paths[:10]):  # Telegram limit: 10 per group
            if fp.exists():
                media.append({
                    "type": "photo",
                    "media": f"attach://img{i}",
                    "caption": text if i == 0 else "",
                    "parse_mode": "Markdown",
                })
                fh = open(fp, "rb")
                open_handles.append(fh)
                files[f"img{i}"] = (fp.name, fh, "image/jpeg")

        if not media:
            return False

        resp = requests.post(
            f"{api_base}/sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media)},
            files=files,
            timeout=60,
        )
        if resp.ok:
            return True
        log.warning("Media group send failed: %s", resp.text[:200])
        # Clear rejection — safe to fall back to text-only.
        return _send_telegram_text(api_base, chat_id, text)
    except requests.Timeout as e:
        log.warning(
            "Media group timeout (may already be delivered); marking uncertain, no text fallback: %s",
            e,
        )
        return None
    except requests.RequestException as e:
        log.warning("Media group error (will retry later, no text fallback): %s", e)
        return False
    finally:
        for fh in open_handles:
            fh.close()


def _send_telegram_text(api_base: str, chat_id: str, text: str) -> bool:
    try:
        resp = requests.post(
            f"{api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        return resp.ok
    except requests.RequestException as e:
        log.error("Message send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Download-and-push helper (handles both images and videos)
# ---------------------------------------------------------------------------


def _record_push_success(
    item_id: int,
    username: str,
    *,
    pushed_ids: set[int] | None,
    pushed_dir: Path | None,
    tg_id: str | None,
) -> None:
    """Mark ``item_id`` as pushed and flush to disk immediately.

    Also clears pending/inflight for the same ID when state dir is available.
    """
    if pushed_ids is None or pushed_dir is None or tg_id is None:
        return
    pushed_ids.add(item_id)
    save_pushed_ids(pushed_dir, tg_id, username, pushed_ids)
    clear_pending(pushed_dir, tg_id, username, item_id)
    clear_inflight(pushed_dir, tg_id, username, item_id)


def _finalize_send_outcome(
    outcome: bool | None,
    item_id: int,
    username: str,
    *,
    pushed_ids: set[int] | None,
    pushed_dir: Path | None,
    tg_id: str | None,
) -> bool:
    """Apply lifecycle bookkeeping for a send outcome. Returns confirmed-success bool."""
    if pushed_dir is not None and tg_id is not None:
        clear_inflight(pushed_dir, tg_id, username, item_id)
    if outcome is True:
        _record_push_success(
            item_id, username,
            pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id,
        )
        return True
    if outcome is None:
        # Uncertain delivery — park as pending; next scan will light-confirm or
        # allow at most one expired retry (see PENDING_MAX_RETRIES).
        if pushed_dir is not None and tg_id is not None:
            existing = load_pending_map(pushed_dir, tg_id, username)
            prev_retries = existing[item_id][1] if item_id in existing else 0
            # If a retry was pre-marked (retries already >= 1), keep that count.
            retries = max(prev_retries, 0)
            mark_pending(pushed_dir, tg_id, username, item_id, retries=retries)
            log.info(
                "Parked id=%d for @%s as pending (uncertain delivery, retries=%d)",
                item_id, username, retries,
            )
        return False
    # Confirmed failure — drop any pending so a later scan can try cleanly.
    if pushed_dir is not None and tg_id is not None:
        clear_pending(pushed_dir, tg_id, username, item_id)
    return False


def process_and_push(
    item: dict[str, Any],
    username: str,
    *,
    size_suffixes: list[str],
    output_dir: Path,
    bot_token: str,
    chat_id: str,
    video_enabled: bool,
    max_video_size_mb: int,
    pushed_ids: set[int] | None = None,
    pushed_dir: Path | None = None,
    tg_id: str | None = None,
) -> bool:
    """Download a single item (image or video) and push to Telegram.

    Pre-claims the item as inflight before the Telegram request so a hard kill
    mid-send leaves a recoverable marker. Confirmed success is flushed to
    ``pushed_ids`` immediately; timeouts become pending instead of false success.
    """
    item_id = item["id"]
    civitai_url = f"https://civitai.com/images/{item_id}"
    created_at = item.get("createdAt", "")

    # Detect video
    is_video = (
        video_enabled
        and (
            item.get("type") == "video"
            or str(item.get("url", "")).lower().endswith((".mp4", ".webm", ".mov"))
        )
    )

    def _send(text: str, files: list[Path] | None) -> bool:
        if pushed_dir is not None and tg_id is not None:
            mark_inflight(pushed_dir, tg_id, username, item_id)
        outcome = send_to_telegram(bot_token, chat_id, text, files)
        return _finalize_send_outcome(
            outcome, item_id, username,
            pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id,
        )

    if is_video:
        # Video path
        video_url = item.get("url") or item.get("meta", {}).get("videoUrl", "")
        if not video_url:
            log.warning("Video %s: no URL found", item_id)
            return False

        filepath = output_dir / "videos" / f"{item_id}.mp4"
        success = download_video(video_url, filepath, max_video_size_mb)

        status = " ✅" if success else " ⚠️（视频下载失败，请在 Civitai 页面查看）"
        text = (
            f"🎥 *New video by @{username}*{status}\n"
            f"🔗 [View on Civitai]({civitai_url})\n"
            f"🕐 {created_at}"
        )
        pushed = _send(text, [filepath] if success else None)
        log.info("Pushed %s %s to @%s | id=%d file=%s success=%s push=%s",
                 "video", "✅" if pushed else "❌", username, item_id,
                 filepath.name if success else "none", success, pushed)
        return pushed

    # Image path
    orig_url = normalize_to_original(item.get("url", ""), size_suffixes)
    ext = os.path.splitext(orig_url.split("/")[-1])[1] or ".jpeg"
    filepath = output_dir / f"{item_id}{ext}"
    success = download_image(orig_url, filepath)

    status = " ✅" if success else " ⚠️（图片下载失败，请在 Civitai 页面查看）"
    text = (
            f"🖼 *New artwork by @{username}*{status}\n"
            f"🔗 [View on Civitai]({civitai_url})\n"
            f"🕐 {created_at}"
        )
    pushed = _send(text, [filepath] if success else None)
    log.info("Pushed %s %s to @%s | id=%d file=%s success=%s push=%s",
             "image", "✅" if pushed else "❌", username, item_id,
             filepath.name if success else "none", success, pushed)
    return pushed


# ---------------------------------------------------------------------------
# Single-page fetch-and-process helper (shared by incremental and full modes)
# ---------------------------------------------------------------------------


def _fetch_and_process_page(
    username: str,
    nsfw_flag: bool | None,
    cursor: str,
    seen_ids: set[int],
    pushed_ids: set[int],
    *,
    base_url: str,
    limit: int,
    sort: str | None = "Newest",
    size_suffixes: list[str],
    output_dir: Path,
    bot_token: str,
    chat_id: str,
    video_enabled: bool,
    max_video_size_mb: int,
    pushed_dir: Path,
    tg_id: str,
) -> tuple[list[dict], set[int], str]:
    """Fetch one page (by cursor), find new items, process and push them.

    Returns (new_items_processed, all_item_ids_on_page, next_cursor).
    The caller is responsible for checkpoint-saving seen_ids and cursor-looping.

    Pending / inflight handling (anti-duplicate edge cases):
      * Leftover inflight (crash mid-send) is adopted as pending.
      * Pending younger than PENDING_CONFIRM_SECONDS → light-confirm: promote
        to pushed without re-sending (assume delivered).
      * Pending older with retries < PENDING_MAX_RETRIES → exactly one retry push
        (retries pre-bumped so a second timeout will not re-arm forever).
      * Pending older with retries >= PENDING_MAX_RETRIES → promote, no more sends.
      * If the page only needed light-confirms/promotes (no real push attempt),
        return an empty "new" list so incremental mode stops paging early.
    """
    items, next_cursor = fetch_page(username, base_url=base_url, limit=limit, cursor=cursor, nsfw=nsfw_flag, sort=sort)
    if not items:
        return [], set(), next_cursor

    page_ids = {img["id"] for img in items}
    # Crash recovery: inflight left by a hard kill becomes pending.
    pending = adopt_stale_inflight(pushed_dir, tg_id, username)
    now = time.time()
    new_on_page = [img for img in items if img["id"] not in pushed_ids]
    did_push_attempt = False

    if new_on_page:
        for img in reversed(new_on_page):
            item_id = img["id"]
            if item_id in pushed_ids:
                continue

            # Light-confirm / capped-retry path for uncertain prior sends.
            if item_id in pending:
                ts, retries = pending[item_id]
                age = now - float(ts)
                if age < PENDING_CONFIRM_SECONDS:
                    log.info(
                        "Pending id=%d for @%s is fresh (%.0fs < %ds); promoting to pushed without re-send",
                        item_id, username, age, PENDING_CONFIRM_SECONDS,
                    )
                    _record_push_success(
                        item_id, username,
                        pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id,
                    )
                    continue
                if retries >= PENDING_MAX_RETRIES:
                    log.info(
                        "Pending id=%d for @%s expired with retries=%d; promoting without further re-send",
                        item_id, username, retries,
                    )
                    _record_push_success(
                        item_id, username,
                        pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id,
                    )
                    continue
                # First expiry: pre-bump retries so a second timeout parks at max
                # and will not schedule yet another re-push next time.
                log.info(
                    "Pending id=%d for @%s expired (%.0fs, retries=%d); one retry push",
                    item_id, username, age, retries,
                )
                mark_pending(
                    pushed_dir, tg_id, username, item_id,
                    ts=now, retries=retries + 1,
                )
                pending[item_id] = (now, retries + 1)

            # process_and_push pre-claims inflight, then records pushed/pending.
            did_push_attempt = True
            process_and_push(
                img, username,
                size_suffixes=size_suffixes,
                output_dir=output_dir,
                bot_token=bot_token,
                chat_id=chat_id,
                video_enabled=video_enabled,
                max_video_size_mb=max_video_size_mb,
                pushed_ids=pushed_ids,
                pushed_dir=pushed_dir,
                tg_id=tg_id,
            )
            time.sleep(2.0 + random.random() * 1.0)

    # Only pending promotes / already-handled items → signal catch-up to caller.
    if not did_push_attempt:
        return [], page_ids, next_cursor
    return new_on_page, page_ids, next_cursor


# ---------------------------------------------------------------------------
# Incremental mode
# ---------------------------------------------------------------------------


def run_incremental(
    username: str,
    *,
    seen_ids: set[int],
    tg_id: str,
    seen_dir: Path,
    nsfw_setting: str,
    output_dir: Path,
    size_suffixes: list[str],
    bot_token: str,
    chat_id: str,
    base_url: str,
    limit: int,
    video_enabled: bool,
    max_video_size_mb: int,
    max_pages: int = 5,
) -> set[int]:
    """Check latest content using cursor pagination.

    Fetches pages until we hit an already-pushed ID (meaning we've caught up
    to previously processed content) or until ``max_pages`` is reached
    (whichever comes first). The page cap exists so that the very first
    run on a creator with a large history (e.g. 1k+ items) does not push
    every historical item to Telegram — that is what ``full`` mode is for.

    Saves seen_ids after each track. Returns the set of all image IDs seen
    on the pages we actually fetched.
    """
    all_seen: set[int] = set(seen_ids)
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = "NSFW" if nsfw_flag else "SFW"
        cursor = ""
        page = 0
        while page < max_pages:
            page += 1
            pushed_ids = load_pushed_ids(seen_dir, tg_id, username)
            new_on_page, page_ids, next_cursor = _fetch_and_process_page(
                username, nsfw_flag, cursor, all_seen, pushed_ids,
                base_url=base_url, limit=limit, sort=None,
                size_suffixes=size_suffixes, output_dir=output_dir,
                bot_token=bot_token, chat_id=chat_id,
                video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
                pushed_dir=seen_dir, tg_id=tg_id,
            )

            if not page_ids:
                break

            # If all items on this page are already pushed, we've caught up
            all_seen.update(page_ids)
            if not new_on_page:
                log.info("%s: caught up after %d pages (track: %s)", username, page, label)
                break

            log.info("%s: +%d new (page %d, track: %s)", username, len(new_on_page), page, label)

            # Save after each page to prevent progress loss
            if all_seen:
                union = seen_ids | all_seen
                if len(union) > len(seen_ids):
                    save_seen_ids(seen_dir, tg_id, username, union)

            # Stop if no more pages
            if not next_cursor:
                break
            cursor = next_cursor

        # Final save for this track
        if all_seen:
            union = seen_ids | all_seen
            if len(union) > len(seen_ids):
                save_seen_ids(seen_dir, tg_id, username, union)

    return all_seen


# ---------------------------------------------------------------------------
# Full mode (backfill)
# ---------------------------------------------------------------------------


def run_full(
    username: str,
    *,
    seen_ids: set[int],
    tg_id: str,
    nsfw_setting: str,
    output_dir: Path,
    size_suffixes: list[str],
    bot_token: str,
    chat_id: str,
    base_url: str,
    limit: int,
    video_enabled: bool,
    max_video_size_mb: int,
    seen_dir: Path,
    shutdown_flag: Callable[[], bool] = lambda: False,
) -> set[int]:
    """Walk every page of the user's gallery for the requested tracks.

    Returns the consolidated set of all image IDs seen. ``shutdown_flag`` is
    a zero-arg callable polled between pages so SIGTERM can stop a long
    backfill promptly without losing already-saved progress.
    """
    all_seen: set[int] = set(seen_ids)
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = "NSFW" if nsfw_flag else "SFW"
        log.info("── %s track for @%s ──", label, username)
        cursor = ""
        page = 0

        while True:
            if shutdown_flag():
                log.info("%s: shutdown requested, stopping at page %d", label, page)
                break
            page += 1
            pushed_ids = load_pushed_ids(seen_dir, tg_id, username)
            new_on_page, page_ids, next_cursor = _fetch_and_process_page(
                username, nsfw_flag, cursor, all_seen, pushed_ids,
                base_url=base_url, limit=limit,
                size_suffixes=size_suffixes, output_dir=output_dir,
                bot_token=bot_token, chat_id=chat_id,
                video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
                pushed_dir=seen_dir, tg_id=tg_id,
            )

            if not page_ids:
                log.info("%s: exhausted after %d pages", label, page - 1)
                break

            all_seen.update(page_ids)

            if new_on_page:
                log.info("%s page %d: +%d new", label, page, len(new_on_page))
            else:
                log.info("%s page %d: all %d already seen", label, page, len(page_ids))

            # Save after every page to prevent progress loss on crash
            save_seen_ids(seen_dir, tg_id, username, all_seen)

            if not next_cursor:
                log.info("%s: completed after %d pages", label, page)
                break
            cursor = next_cursor
            time.sleep(2.0 + random.random() * 1.0)

    return all_seen


# ---------------------------------------------------------------------------
# Cache cleanup
# ---------------------------------------------------------------------------


def cleanup_old_caches(output_dir: Path, keep_days: int, max_total_gb: int = 0) -> int:
    """按天数 + 按总大小双重清理。返回删除的文件数量。"""
    if not output_dir.exists():
        return 0

    removed = 0

    # 第一步：按天数清理
    if keep_days > 0:
        cutoff = time.time() - keep_days * 86400
        for root, dirs, files in os.walk(output_dir):
            for fname in files:
                fpath = Path(root) / fname
                try:
                    if fpath.stat().st_mtime < cutoff:
                        fpath.unlink()
                        removed += 1
                except OSError:
                    pass
            for dname in dirs:
                dpath = Path(root) / dname
                try:
                    if not any(dpath.iterdir()):
                        dpath.rmdir()
                except OSError:
                    pass

    # 第二步：按总大小清理（如果设置了上限）
    if max_total_gb > 0:
        max_bytes = max_total_gb * 1024 * 1024 * 1024
        while True:
            all_files = []
            for root, dirs, files in os.walk(output_dir):
                for fname in files:
                    fpath = Path(root) / fname
                    try:
                        all_files.append((fpath.stat().st_mtime, fpath.stat().st_size, fpath))
                    except OSError:
                        pass

            total_size = sum(f[1] for f in all_files)
            if total_size <= max_bytes:
                break

            # 删除最旧的文件
            all_files.sort()  # 按 mtime 排序，最旧的在前
            oldest = all_files[0][2]
            try:
                oldest.unlink()
                removed += 1
            except OSError:
                break

    return removed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# Graceful shutdown flag — set by SIGTERM/SIGINT handler so the main loop
# can finish the current page (saving seen_ids) before exiting, rather than
# losing in-flight progress.
_monitor_shutdown_requested = False


def _monitor_signal_handler(signum, _frame):
    global _monitor_shutdown_requested
    if _monitor_shutdown_requested:
        return  # second signal → force exit on next call site
    _monitor_shutdown_requested = True
    log.info("monitor.py: signal %d received, finishing current page then exiting", signum)


signal.signal(signal.SIGTERM, _monitor_signal_handler)
signal.signal(signal.SIGINT, _monitor_signal_handler)


def _acquire_process_lock() -> int | None:
    """Acquire the .monitor.lock file with an exclusive fcntl lock.

    Returns the file descriptor on success, or None if another monitor.py
    process already holds the lock (in which case the caller should exit 75
    so the bot's scheduled cron knows to back off).

    Cleanup is registered with atexit: the lock is released and the
    sentinel file is removed when this process exits, even on hard kills.
    """
    lock_file = SCRIPT_DIR / LOCK_FILE_NAME
    try:
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        try:
            os.close(lock_fd)
        except (OSError, UnboundLocalError):
            pass
        return None

    # Write PID to lock file so _is_scan_running can detect stale locks
    try:
        os.ftruncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode())
        os.fsync(lock_fd)
    except OSError:
        pass

    def _release_lock() -> None:
        try:
            os.close(lock_fd)
        except OSError:
            pass
        try:
            os.unlink(lock_file)
        except OSError:
            pass
    atexit.register(_release_lock)
    return lock_fd


def _is_scan_running() -> bool:
    # Check if a monitor scan is already running by probing the lock file PID.
    # Reads the PID from .monitor.lock and checks if that process is alive.
    # This catches stale locks left by SIGKILL where the kernel released
    # the flock but the sentinel file was not unlinked by the atexit handler.
    lock_file = SCRIPT_DIR / LOCK_FILE_NAME
    try:
        pid_str = lock_file.read_text().strip()
        if pid_str:
            pid = int(pid_str)
            os.kill(pid, 0)
            return True
    except (ValueError, ProcessLookupError, OSError, FileNotFoundError):
        pass
    return False


def _write_status(
    *,
    start_time: "_dt.datetime",
    mode: str,
    current_creator: str,
    creators_done: int,
    creators_total: int,
    pushed_count: int,
) -> None:
    """Write a single status snapshot for /status consumers to read."""
    payload = {
        "status": "running",
        "started_at": start_time.strftime("%H:%M:%S"),
        "mode": mode,
        "current_creator": current_creator,
        "creators_done": creators_done,
        "creators_total": creators_total,
        "pushed_count": pushed_count,
        "elapsed_seconds": int((_dt.datetime.now() - start_time).total_seconds()),
    }
    STATUS_PATH.write_text(json.dumps(payload))


def _clear_status(interrupted: bool, start_time: "_dt.datetime") -> None:
    """Remove the status file, optionally leaving an "interrupted" snapshot
    for operators to inspect. Safe to call from a finally block."""
    try:
        if interrupted:
            STATUS_PATH.write_text(json.dumps({
                "status": "interrupted",
                "elapsed_seconds": int((_dt.datetime.now() - start_time).total_seconds()),
            }))
        STATUS_PATH.unlink()
    except OSError:
        pass


def _build_backfill_summary(username: str, new_count: int, mode: str, nsfw: str, video_enabled: bool) -> str:
    """Compose the Telegram message that announces a backfill completion."""
    return (
        f"✅ *Backfill complete for @{username}*\n"
        f"📸 Mode: {mode} · NSFW: {nsfw} · Video: {video_enabled}\n"
        f"🆕 New items: {new_count}"
    )


def _process_single_creator(
    username: str,
    tg_id_str: str,
    seen_dir: Path,
    output_dir: Path,
    *,
    mode: str,
    nsfw: str,
    size_suffixes: list[str],
    bot_token: str,
    chat_id: str,
    base_url: str,
    page_limit: int,
    video_enabled: bool,
    max_video_size_mb: int,
    incremental_max_pages: int,
) -> tuple[set[int], int]:
    """Run a single creator through incremental or full mode.

    Returns ``(all_seen, new_count)`` where ``new_count`` is how many IDs
    were net-new compared to the on-disk seen_ids file.
    """
    seen_ids = load_seen_ids(seen_dir, tg_id_str, username)
    common: dict[str, Any] = {
        "username": username,
        "seen_ids": seen_ids,
        "tg_id": tg_id_str,
        "seen_dir": seen_dir,
        "nsfw_setting": nsfw,
        "output_dir": output_dir,
        "size_suffixes": size_suffixes,
        "bot_token": bot_token,
        "chat_id": chat_id,
        "base_url": base_url,
        "limit": page_limit,
        "video_enabled": video_enabled,
        "max_video_size_mb": max_video_size_mb,
    }
    if mode == "full":
        user_seen = run_full(
            **common,
            shutdown_flag=lambda: _monitor_shutdown_requested,
        )
    else:
        user_seen = run_incremental(**common, max_pages=incremental_max_pages)

    if not user_seen:
        return seen_ids, 0
    union = seen_ids | user_seen
    if len(union) <= len(seen_ids):
        return seen_ids, 0
    save_seen_ids(seen_dir, tg_id_str, username, union)
    new_count = len(union) - len(seen_ids)
    log.info("Merged %d new IDs for @%s (TG:%s) (total: %d)",
             new_count, username, tg_id_str, len(union))
    return union, new_count


def main() -> None:
    # Step 1: process lock. Bails out cleanly if another monitor.py is running.
    if _acquire_process_lock() is None:
        log.warning("Another monitor process is already running - skipping this cron tick")
        sys.exit(75)

    # Step 2: parse CLI + load config
    parser = argparse.ArgumentParser(description="Civitai Monitor — civitai.com user gallery monitor")
    parser.add_argument("--config", type=str, help="Path to config.yaml (default: auto-search)")
    parser.add_argument("--mode", type=str, choices=["incremental", "full"], help="Override scan mode")
    parser.add_argument("--user", type=str, help="Process only this Civitai username")
    args = parser.parse_args()

    global cfg  # noqa: PLW0602
    cfg = load_config(Path(args.config) if args.config else None)
    if cfg is None:
        sys.exit(1)
    global _tg_api_base
    _tg_api_base = cfg.telegram.api_base_url
    if args.mode:
        cfg.mode = args.mode

    log.info("Mode: %s | NSFW: %s | Video: %s | Incremental max_pages: %d",
             cfg.mode, cfg.nsfw, cfg.video_enabled, cfg.incremental.max_pages)

    # Step 3: prepare filesystem layout
    data_dir = Path(cfg.data.data_dir) if cfg.data.data_dir else SCRIPT_DIR
    output_dir = data_dir / cfg.download.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    seen_dir = data_dir / DATA_DIR_NAME
    seen_dir.mkdir(parents=True, exist_ok=True)

    subs = cfg.subscriptions or {}
    if not subs:
        log.error("No subscriptions configured in config.yaml")
        sys.exit(1)

    # Step 4: write initial status, then walk every (TG user, creator) pair.
    start_time = _dt.datetime.now()
    total_creators = sum(len(v) for v in subs.values())
    _write_status(
        start_time=start_time, mode=cfg.mode, current_creator="",
        creators_done=0, creators_total=total_creators, pushed_count=0,
    )

    pushed_count = 0
    creator_idx = 0
    try:
        for tg_id, user_list in subs.items():
            if _monitor_shutdown_requested:
                log.info("monitor.py: shutdown requested, stopping user loop")
                break
            tg_id_str = str(tg_id)
            for entry in user_list:
                if _monitor_shutdown_requested:
                    log.info("monitor.py: shutdown requested, stopping creator loop")
                    break
                username = entry.get("name", str(entry)) if isinstance(entry, dict) else str(entry)
                if args.user and username != args.user:
                    log.info("Skipping @%s (--user filter active)", username)
                    continue
                creator_idx += 1
                _write_status(
                    start_time=start_time, mode=cfg.mode, current_creator=username,
                    creators_done=creator_idx, creators_total=total_creators,
                    pushed_count=pushed_count,
                )
                log.info("=" * 50)
                log.info("Processing @%s (TG:%s, %s mode)...", username, tg_id_str, cfg.mode)
                _seen_after, new_count = _process_single_creator(
                    username, tg_id_str, seen_dir, output_dir,
                    mode=cfg.mode, nsfw=cfg.nsfw,
                    size_suffixes=cfg.download.size_suffixes,
                    bot_token=cfg.telegram.bot_token,
                    chat_id=cfg.telegram.chat_id,
                    base_url=cfg.api.base_url,
                    page_limit=cfg.api.images_per_page,
                    video_enabled=cfg.video_enabled,
                    max_video_size_mb=cfg.max_video_size_mb,
                    incremental_max_pages=cfg.incremental.max_pages,
                )
                pushed_count += new_count
                # Per-user completion message in full mode
                if cfg.mode == "full" and new_count > 0:
                    log.info("Full backfill complete for @%s (TG:%s): %d new items",
                             username, tg_id_str, new_count)
                    send_to_telegram(
                        cfg.telegram.bot_token, cfg.telegram.chat_id,
                        _build_backfill_summary(username, new_count, cfg.mode, cfg.nsfw, cfg.video_enabled),
                    )
    finally:
        # Status is cleared even on early shutdown so /status never sees a
        # stale "running" file forever.
        _clear_status(_monitor_shutdown_requested, start_time)

    # Step 5: cache cleanup. Skipped if we were interrupted — exit fast instead.
    if not _monitor_shutdown_requested:
        removed = cleanup_old_caches(output_dir, cfg.download.keep_days, cfg.download.max_total_gb)
        if removed:
            log.info("Cleaned %d cached files older than %d days",
                     removed, cfg.download.keep_days)


if __name__ == "__main__":
    main()
