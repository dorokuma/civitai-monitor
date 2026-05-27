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

import argparse
import fcntl
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential, wait_random, retry_if_exception_type

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("civitai-monitor")

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
    size_suffixes: list[str] = Field(
        default=["/width=1024/", "/width=450/", "/width=640/"]
    )


class ApiConfig(BaseModel):
    base_url: str = "https://civitai.com/api/v1"
    images_per_page: int = 100


class TelegramConfig(BaseModel):
    bot_token: str
    chat_id: str


class DataConfig(BaseModel):
    data_dir: str = ""
    seen_ids_file: str = "seen_ids.json"


class MonitorConfig(BaseModel):
    users: list = Field(default_factory=list)
    subscriptions: dict[str, list] = Field(default_factory=dict)
    authorized_users: list[int] = Field(default_factory=list)
    mode: str = "incremental"
    nsfw: str = "both"
    api: ApiConfig = Field(default_factory=ApiConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    telegram: TelegramConfig
    data: DataConfig = Field(default_factory=DataConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
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

# ---------------------------------------------------------------------------
# Global HTTP session (enforces Referer + User-Agent on every request)
# ---------------------------------------------------------------------------

session = requests.Session()


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


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=30) + wait_random(min=0, max=3),
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


def load_config(path: Path | None = None) -> MonitorConfig:
    paths = [Path(path)] if path else DEFAULT_CONFIG_PATHS
    for p in paths:
        if p.exists():
            log.info("Loading config from %s", p)
            with open(p, encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            try:
                cfg = MonitorConfig(**raw)
                init_session(cfg.http)
                return cfg
            except ValidationError as e:
                log.error("Config validation error:\n%s", e)
                print(json.dumps({"error": "Config validation failed", "details": str(e)}))
                sys.exit(1)
    log.error("config.yaml not found (searched: %s)", [str(p) for p in paths])
    print(json.dumps({"error": "config.yaml not found", "searched": [str(p) for p in paths]}))
    sys.exit(1)


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
        params["browsingLevel"] = 8
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
                "%s: sort=Newest returned 0 items for page %d (nsfw=%s), "
                "retrying with default sort",
                username, page, nsfw,
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


LOCK_PATH = SCRIPT_DIR / "seen_ids.lock"


def seen_file_for_user(seen_dir: Path, tg_id: str, username: str) -> Path:
    """Get the per-user seen IDs file path.

    Each (Telegram user, Civitai user) pair has its own independent file
    so that different Telegram accounts have separate download progress.
    """
    seen_dir.mkdir(parents=True, exist_ok=True)
    return seen_dir / f"seen_ids_{tg_id}_{username}.json"


def load_seen_ids(seen_dir: Path, tg_id: str, username: str) -> set[int]:
    """Load seen IDs for a specific (Telegram user, Civitai user) pair."""
    path = seen_file_for_user(seen_dir, tg_id, username)
    return set(json.loads(path.read_text())) if path.exists() else set()


def save_seen_ids(seen_dir: Path, tg_id: str, username: str, ids: set[int]) -> None:
    """Save seen IDs for a specific (Telegram user, Civitai user) pair."""
    path = seen_file_for_user(seen_dir, tg_id, username)
    lock = FileLock(str(LOCK_PATH), timeout=10)
    try:
        with lock:
            path.write_text(json.dumps(sorted(ids), indent=2))
        log.info("Saved %d seen IDs for @%s", len(ids), username)
    except Timeout:
        log.warning("Timeout saving %d seen IDs for @%s, skipped", len(ids), username)


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------


def download_image(url: str, save_path: Path, timeout: int = 120) -> bool:
    try:
        resp = safe_get(url, timeout=timeout)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(resp.content)
        log.info("Downloaded: %s (%d bytes)", save_path.name, len(resp.content))
        return True
    except requests.RequestException as e:
        log.warning("Image download failed for %s: %s", url, e)
        return False


# ---------------------------------------------------------------------------
# Video download (streaming + size check)
# ---------------------------------------------------------------------------


def download_video(url: str, save_path: Path, max_size_mb: int = 1024) -> bool:
    """Download a full-quality video from Civitai CDN.

    Strategy:
      1. Follow redirect from image.civitai.com → B2 /default (cover)
      2. Rewrite /default → /original on B2 to get the real video
      3. If /original is 404 (video no longer available), skip
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

        # Check size
        cl = resp.headers.get("content-length")
        if cl and int(cl) > max_size_mb * 1024 * 1024:
            log.warning("Video too large (%.1f MB > %d MB), skipping",
                        int(cl) / 1024 / 1024, max_size_mb)
            return False

        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        log.info("Video downloaded: %s (%.1f MB)", save_path.name,
                 save_path.stat().st_size / 1024 / 1024)
        return True
    except requests.RequestException as e:
        log.warning("Video download failed for %s: %s", url, e)
        return False


# ---------------------------------------------------------------------------
# Telegram push
# ---------------------------------------------------------------------------


def send_to_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None = None,
) -> bool:
    api_base = f"https://api.telegram.org/bot{bot_token}"

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


def _send_telegram_video(api_base: str, chat_id: str, text: str, video_path: Path) -> bool:
    """Send a video to Telegram. Falls back to text on error (Telegram caps at 50 MB)."""
    try:
        with open(video_path, "rb") as f:
            resp = requests.post(
                f"{api_base}/sendVideo",
                data={"chat_id": chat_id, "caption": text, "parse_mode": "Markdown"},
                files={"video": (video_path.name, f, "video/mp4")},
                timeout=300,
            )
        if resp.ok:
            return True
        log.warning("Video send failed: %s", resp.text[:200])
    except requests.RequestException as e:
        log.warning("Video send error: %s", e)
    return _send_telegram_text(api_base, chat_id, text)


def _send_telegram_media_group(api_base: str, chat_id: str, text: str, file_paths: list[Path]) -> bool:
    media = []
    files: dict[str, tuple] = {}
    for i, fp in enumerate(file_paths[:10]):  # Telegram limit: 10 per group
        if fp.exists():
            media.append({
                "type": "photo",
                "media": f"attach://img{i}",
                "caption": text if i == 0 else "",
                "parse_mode": "Markdown",
            })
            files[f"img{i}"] = (fp.name, fp.read_bytes(), "image/jpeg")

    if not media:
        return False

    try:
        resp = requests.post(
            f"{api_base}/sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media)},
            files=files,
            timeout=60,
        )
        if resp.ok:
            return True
        log.warning("Media group send failed: %s", resp.text[:200])
    except requests.RequestException as e:
        log.warning("Media group error: %s", e)
    return _send_telegram_text(api_base, chat_id, text)


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
) -> bool:
    """Download a single item (image or video) and push to Telegram."""
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
        pushed = send_to_telegram(bot_token, chat_id, text, [filepath] if success else None)
        log.info("Pushed %s %s to @%s | id=%d file=%s success=%s push=%s",
                 "video", "✅" if pushed else "❌", username, item_id,
                 filepath.name if success else "none", success, pushed)
        return success

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
    pushed = send_to_telegram(bot_token, chat_id, text, [filepath] if success else None)
    log.info("Pushed %s %s to @%s | id=%d file=%s success=%s push=%s",
             "image", "✅" if pushed else "❌", username, item_id,
             filepath.name if success else "none", success, pushed)
    return success


# ---------------------------------------------------------------------------
# Single-page fetch-and-process helper (shared by incremental and full modes)
# ---------------------------------------------------------------------------


def _fetch_and_process_page(
    username: str,
    nsfw_flag: bool | None,
    cursor: str,
    seen_ids: set[int],
    *,
    base_url: str,
    limit: int,
    size_suffixes: list[str],
    output_dir: Path,
    bot_token: str,
    chat_id: str,
    video_enabled: bool,
    max_video_size_mb: int,
) -> tuple[list[dict], set[int], str]:
    """Fetch one page (by cursor), find new items, process and push them.

    Returns (new_items_processed, all_item_ids_on_page, next_cursor).
    The caller is responsible for checkpoint-saving seen_ids and cursor-looping.
    """
    items, next_cursor = fetch_page(username, base_url=base_url, limit=limit, cursor=cursor, nsfw=nsfw_flag)
    if not items:
        return [], set(), next_cursor

    page_ids = {img["id"] for img in items}
    new_on_page = [img for img in items if img["id"] not in seen_ids]

    if new_on_page:
        for img in reversed(new_on_page):
            process_and_push(
                img, username,
                size_suffixes=size_suffixes,
                output_dir=output_dir,
                bot_token=bot_token,
                chat_id=chat_id,
                video_enabled=video_enabled,
                max_video_size_mb=max_video_size_mb,
            )
            time.sleep(0.5)

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
) -> set[int]:
    """Check latest content using cursor pagination.

    Fetches pages until we hit an already-seen ID (meaning we've caught up
    to previously processed content). Saves seen_ids after each track.
    Returns the set of all image IDs seen on the latest page(s).
    """
    all_seen: set[int] = set(seen_ids)
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = "NSFW" if nsfw_flag else "SFW"
        cursor = ""
        page = 0
        while True:
            page += 1
            new_on_page, page_ids, next_cursor = _fetch_and_process_page(
                username, nsfw_flag, cursor, all_seen,
                base_url=base_url, limit=limit,
                size_suffixes=size_suffixes, output_dir=output_dir,
                bot_token=bot_token, chat_id=chat_id,
                video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
            )

            if not page_ids:
                break

            # If all items on this page are already seen, we've caught up
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
) -> set[int]:
    """Walk every page of the user's gallery for the requested tracks.

    Returns the consolidated set of all image IDs seen.
    """
    all_seen: set[int] = set(seen_ids)
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = "NSFW" if nsfw_flag else "SFW"
        log.info("── %s track for @%s ──", label, username)
        cursor = ""
        page = 0

        while True:
            page += 1
            new_on_page, page_ids, next_cursor = _fetch_and_process_page(
                username, nsfw_flag, cursor, all_seen,
                base_url=base_url, limit=limit,
                size_suffixes=size_suffixes, output_dir=output_dir,
                bot_token=bot_token, chat_id=chat_id,
                video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
            )

            if not page_ids:
                log.info("%s: exhausted after %d pages", label, page - 1)
                break

            all_seen.update(page_ids)

            if new_on_page:
                log.info("%s page %d: +%d new", label, page, len(new_on_page))
            else:
                log.info("%s page %d: all %d already seen", label, page, len(page_ids))

            # Save progress periodically
            if page % 10 == 0:
                save_seen_ids(seen_dir, tg_id, username, all_seen)

            if not next_cursor:
                log.info("%s: completed after %d pages", label, page)
                break
            cursor = next_cursor
            time.sleep(0.5)

    return all_seen


# ---------------------------------------------------------------------------
# Cache cleanup
# ---------------------------------------------------------------------------


def cleanup_old_caches(output_dir: Path, keep_days: int) -> int:
    """Remove cached files older than keep_days. Returns count removed."""
    if keep_days <= 0 or not output_dir.exists():
        return 0

    cutoff = time.time() - keep_days * 86400
    removed = 0
    for f in output_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1

    # Also clean videos subdirectory
    video_dir = output_dir / "videos"
    if video_dir.exists():
        for f in video_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1

    return removed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    # Process lock: prevent concurrent cron runs
    lock_file = SCRIPT_DIR / ".monitor.lock"
    try:
        lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        log.warning("Another monitor process is already running - skipping this cron tick")
        try:
            os.close(lock_fd)
        except Exception:
            pass
        sys.exit(0)

    parser = argparse.ArgumentParser(description="Civitai Monitor — civitai.com user gallery monitor")
    parser.add_argument("--config", type=str, help="Path to config.yaml (default: auto-search)")
    parser.add_argument("--mode", type=str, choices=["incremental", "full"], help="Override scan mode")
    parser.add_argument("--user", type=str, help="Process only this Civitai username")
    args = parser.parse_args()

    global cfg  # noqa: PLW0602
    cfg = load_config(Path(args.config) if args.config else None)
    if args.mode:
        cfg.mode = args.mode

    log.info("Mode: %s | NSFW: %s | Video: %s", cfg.mode, cfg.nsfw, cfg.video_enabled)

    # -- Paths --
    data_dir = Path(cfg.data.data_dir) if cfg.data.data_dir else SCRIPT_DIR
    output_dir = data_dir / cfg.download.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    seen_dir = data_dir / "seen_ids"
    seen_dir.mkdir(parents=True, exist_ok=True)

    # -- Per-Telegram-user processing (each TG user has independent seen_ids) --
    subs = cfg.subscriptions or {}
    if not subs:
        log.error("No subscriptions configured in config.yaml")
        sys.exit(1)

    for tg_id, user_list in subs.items():
        tg_id_str = str(tg_id)
        for entry in user_list:
            username = entry.get("name", str(entry)) if isinstance(entry, dict) else str(entry)
            if args.user and username != args.user:
                log.info("Skipping @%s (--user filter active)", username)
                continue
            log.info("=" * 50)
            log.info("Processing @%s (TG:%s, %s mode)...", username, tg_id_str, cfg.mode)

            # Load this (TG user, Civitai user) pair's own seen IDs
            seen_ids = load_seen_ids(seen_dir, tg_id_str, username)

            if cfg.mode == "full":
                user_seen = run_full(
                    username,
                    seen_ids=seen_ids,
                    nsfw_setting=cfg.nsfw,
                    output_dir=output_dir,
                    size_suffixes=cfg.download.size_suffixes,
                    bot_token=cfg.telegram.bot_token,
                    chat_id=cfg.telegram.chat_id,
                    base_url=cfg.api.base_url,
                    limit=cfg.api.images_per_page,
                    video_enabled=cfg.video_enabled,
                    max_video_size_mb=cfg.max_video_size_mb,
                    seen_dir=seen_dir,
                    tg_id=tg_id_str,
                )
            else:
                user_seen = run_incremental(
                    username,
                    seen_ids=seen_ids,
                    tg_id=tg_id_str,
                    seen_dir=seen_dir,
                    nsfw_setting=cfg.nsfw,
                    output_dir=output_dir,
                    size_suffixes=cfg.download.size_suffixes,
                    bot_token=cfg.telegram.bot_token,
                    chat_id=cfg.telegram.chat_id,
                    base_url=cfg.api.base_url,
                    limit=cfg.api.images_per_page,
                    video_enabled=cfg.video_enabled,
                    max_video_size_mb=cfg.max_video_size_mb,
                )

            # Save per-(TG user, Civitai user) progress immediately
            if user_seen:
                union = seen_ids | user_seen
                if len(union) > len(seen_ids):
                    save_seen_ids(seen_dir, tg_id_str, username, union)
                    new_count = len(union) - len(seen_ids)
                    log.info("Merged %d new IDs for @%s (TG:%s) (total: %d)", new_count, username, tg_id_str, len(union))

                    # Full mode: per-user completion message
                    if cfg.mode == "full":
                        log.info("Full backfill complete for @%s (TG:%s): %d new items", username, tg_id_str, new_count)
                        summary = (
                            f"✅ *Backfill complete for @{username}*\n"
                            f"📸 Mode: {cfg.mode} · NSFW: {cfg.nsfw} · Video: {cfg.video_enabled}\n"
                            f"🆕 New items: {new_count}"
                        )
                        send_to_telegram(cfg.telegram.bot_token, cfg.telegram.chat_id, summary)

    # -- Cleanup old caches --
    removed = cleanup_old_caches(output_dir, cfg.download.keep_days)
    if removed:
        log.info("Cleaned %d cached files older than %d days", removed, cfg.download.keep_days)


if __name__ == "__main__":
    main()
