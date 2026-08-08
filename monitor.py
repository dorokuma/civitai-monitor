#!/usr/bin/env python3

"""
Civitai Monitor — Enhanced Edition

Monitors specified Civitai users for new image & video uploads,
downloads full-resolution originals, and pushes them to a Telegram
channel via the Bot API.

Orchestration layer: config / HTTP client / state / Telegram media live
in sibling modules and are re-exported here for backward compatibility
(`from monitor import X` and existing tests).

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
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable

import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)
log = logging.getLogger("civitai-monitor")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Re-exports (keep `from monitor import X` stable for bot + tests)
# ---------------------------------------------------------------------------

from config_io import (  # noqa: E402
    ApiConfig,
    BackfillConfig,
    DATA_DIR_NAME,
    DEFAULT_CONFIG_PATHS,
    DataConfig,
    DownloadConfig,
    HttpConfig,
    IncrementalConfig,
    LOCK_FILE_NAME,
    MonitorConfig,
    SCRIPT_DIR,
    STATUS_FILE_NAME,
    TelegramConfig,
    VALID_MODES,
    VALID_NSFW,
    load_config,
    redact_config_for_disk,
    write_config,
)
from civitai_client import (  # noqa: E402
    FetchPageError,
    HTTP_REQUEST_TIMEOUT,
    MAX_API_PAGE_LIMIT,
    MIN_API_PAGE_LIMIT,
    RateLimitError,
    fetch_page,
    init_session,
    safe_get,
    session,
)
from state_store import (  # noqa: E402
    PENDING_CONFIRM_SECONDS,
    PENDING_MAX_RETRIES,
    PendingMap,
    adopt_stale_inflight,
    clear_inflight,
    clear_pending,
    load_pending_map,
    load_push_timestamps,
    load_pushed_ids,
    load_seen_ids,
    mark_inflight,
    mark_pending,
    pushed_file_for_user,
    save_pushed_ids,
    save_seen_ids,
    seen_file_for_user,
    update_pending_map,
    update_push_timestamps,
)
from telegram_media import (  # noqa: E402
    TELEGRAM_DOCUMENT_MAX_BYTES,
    TELEGRAM_PHOTO_MAX_BYTES,
    TELEGRAM_VIDEO_MAX_MB,
    _send_telegram_media_group,
    _send_telegram_text,
    _send_telegram_video,
    escape_markdown,
    get_tg_api_base,
    send_to_telegram,
    set_tg_api_base,
)

# Re-export for callers that still read monitor._tg_api_base-style names.
# Prefer set_tg_api_base() / get_tg_api_base().

# Per-item timeouts (seconds) for downloads
IMAGE_DOWNLOAD_TIMEOUT = 120
VIDEO_DOWNLOAD_TIMEOUT = 120

LOCK_PATH = SCRIPT_DIR / LOCK_FILE_NAME
STATUS_PATH = SCRIPT_DIR / STATUS_FILE_NAME


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
# Image download
# ---------------------------------------------------------------------------


def download_image(url: str, save_path: Path, timeout: int = 120) -> bool:
    if save_path.exists():
        log.info("Already exists: %s, skipped", save_path.name)
        return True
    try:
        with safe_get(url, stream=True, timeout=timeout) as resp:
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
    """Download a full-quality video from Civitai CDN.

    Strategy:
      1. Follow redirect from image.civitai.com → B2 /default (cover)
      2. Rewrite /default → /original on B2 to get the real video
      3. If /original is 404 (video no longer available), skip

    ``max_size_mb`` semantics:
      * ``> 0``  — skip videos whose Content-Length exceeds the cap; when CL is
                   missing, count bytes while streaming and abort past the cap.
      * ``<= 0`` — disable the size cap (download every video regardless of size).
    """
    if save_path.exists():
        log.info("Already exists: %s, skipped", save_path.name)
        return True
    try:
        with safe_get(url, stream=True) as resp:
            resp.raise_for_status()
            b2_url = str(resp.url)

        if "image-b2.civitai.com" in b2_url and b2_url.endswith("/default"):
            resolved_url = b2_url[:-8] + "/original"
            log.info("B2: /default → /original")
            content_url = resolved_url
        else:
            log.info("Non-B2 video CDN: %s...", b2_url[:80])
            content_url = url

        last_status = None
        max_bytes = max_size_mb * 1024 * 1024 if max_size_mb > 0 else 0
        for attempt in range(3):
            with safe_get(content_url, stream=True, timeout=120) as resp:
                if resp.status_code == 200:
                    cl = resp.headers.get("content-length")
                    cl_int = 0
                    if cl:
                        try:
                            cl_int = int(cl)
                        except ValueError:
                            log.warning(
                                "Video Content-Length %r unparseable, treating as unknown size",
                                cl,
                            )
                    if max_bytes and cl_int > max_bytes:
                        log.warning(
                            "Video too large (%.1f MB > %d MB), skipping",
                            cl_int / 1024 / 1024, max_size_mb,
                        )
                        return False
                    if not cl and max_bytes:
                        log.info(
                            "Video size unknown (no Content-Length), streaming with %d MB cap",
                            max_size_mb,
                        )
                    save_path.parent.mkdir(parents=True, exist_ok=True)
                    tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")
                    downloaded = 0
                    try:
                        with open(tmp_path, "wb") as f:
                            for chunk in resp.iter_content(chunk_size=8192):
                                if not chunk:
                                    continue
                                downloaded += len(chunk)
                                if max_bytes and downloaded > max_bytes:
                                    log.warning(
                                        "Video exceeded cap while streaming "
                                        "(%.1f MB > %d MB), deleting tmp",
                                        downloaded / 1024 / 1024, max_size_mb,
                                    )
                                    f.flush()
                                    raise _VideoSizeCapExceeded()
                                f.write(chunk)
                        tmp_path.rename(save_path)
                        log.info(
                            "Video downloaded: %s (%.1f MB)",
                            save_path.name,
                            save_path.stat().st_size / 1024 / 1024,
                        )
                        return True
                    except _VideoSizeCapExceeded:
                        try:
                            tmp_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return False
                last_status = resp.status_code
                if resp.status_code == 404:
                    log.warning("B2 /original 404 — video file not available on CDN")
                    return False
                if attempt < 2:
                    log.info(
                        "B2 /original returned %d, retrying (%d/3)...",
                        resp.status_code, attempt + 2,
                    )
                    time.sleep(2 ** attempt)
        raise requests.HTTPError(f"Video fetch failed (last status {last_status})")
    except requests.RequestException as e:
        log.warning("Video download failed for %s: %s", url, e)
        return False


class _VideoSizeCapExceeded(Exception):
    """Internal: stream exceeded max_video_size_mb without Content-Length."""


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
    """Mark ``item_id`` as pushed and flush to disk immediately."""
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
        if pushed_dir is not None and tg_id is not None:
            existing = load_pending_map(pushed_dir, tg_id, username)
            prev_retries = existing[item_id][1] if item_id in existing else 0
            retries = max(prev_retries, 0)
            mark_pending(pushed_dir, tg_id, username, item_id, retries=retries)
            log.info(
                "Parked id=%d for @%s as pending (uncertain delivery, retries=%d)",
                item_id, username, retries,
            )
        return False
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
    """Download a single item (image or video) and push to Telegram."""
    item_id = item["id"]
    civitai_url = f"https://civitai.com/images/{item_id}"
    created_at = item.get("createdAt", "")
    safe_user = escape_markdown(username)

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
        video_url = item.get("url") or item.get("meta", {}).get("videoUrl", "")
        if not video_url:
            log.warning("Video %s: no URL found", item_id)
            return False

        filepath = output_dir / "videos" / f"{item_id}.mp4"
        success = download_video(video_url, filepath, max_video_size_mb)

        status = " ✅" if success else " ⚠️（视频下载失败，请在 Civitai 页面查看）"
        text = (
            f"🎥 *New video by @{safe_user}*{status}\n"
            f"🔗 [View on Civitai]({civitai_url})\n"
            f"🕐 {created_at}"
        )
        pushed = _send(text, [filepath] if success else None)
        log.info(
            "Pushed %s %s to @%s | id=%d file=%s success=%s push=%s",
            "video", "✅" if pushed else "❌", username, item_id,
            filepath.name if success else "none", success, pushed,
        )
        return pushed

    orig_url = normalize_to_original(item.get("url", ""), size_suffixes)
    ext = os.path.splitext(orig_url.split("/")[-1])[1] or ".jpeg"
    filepath = output_dir / f"{item_id}{ext}"
    success = download_image(orig_url, filepath)

    status = " ✅" if success else " ⚠️（图片下载失败，请在 Civitai 页面查看）"
    text = (
        f"🖼 *New artwork by @{safe_user}*{status}\n"
        f"🔗 [View on Civitai]({civitai_url})\n"
        f"🕐 {created_at}"
    )
    pushed = _send(text, [filepath] if success else None)
    log.info(
        "Pushed %s %s to @%s | id=%d file=%s success=%s push=%s",
        "image", "✅" if pushed else "❌", username, item_id,
        filepath.name if success else "none", success, pushed,
    )
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

    Propagates ``FetchPageError`` from ``fetch_page`` (network/HTTP hard fail).
    """
    items, next_cursor = fetch_page(
        username, base_url=base_url, limit=limit, cursor=cursor, nsfw=nsfw_flag, sort=sort,
    )
    if not items:
        return [], set(), next_cursor

    page_ids = {img["id"] for img in items}
    pending = adopt_stale_inflight(pushed_dir, tg_id, username)
    now = time.time()
    new_on_page = [img for img in items if img["id"] not in pushed_ids]
    did_push_attempt = False

    for item_id, (ts, retries) in list(pending.items()):
        if item_id in page_ids:
            continue
        age = now - float(ts)
        if age < PENDING_CONFIRM_SECONDS:
            _record_push_success(
                item_id, username,
                pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id,
            )
            continue
        log.info(
            "Pending id=%d for @%s is off-window and expired (%.0fs, retries=%d); "
            "promoting without re-send",
            item_id, username, age, retries,
        )
        _record_push_success(
            item_id, username,
            pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id,
        )

    if new_on_page:
        for img in reversed(new_on_page):
            item_id = img["id"]
            if item_id in pushed_ids:
                continue

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
                log.info(
                    "Pending id=%d for @%s expired (%.0fs, retries=%d); one retry push",
                    item_id, username, age, retries,
                )
                mark_pending(
                    pushed_dir, tg_id, username, item_id,
                    ts=now, retries=retries + 1,
                )
                pending[item_id] = (now, retries + 1)

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
    """Check latest content using cursor pagination. Propagates FetchPageError."""
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

            all_seen.update(page_ids)
            if not new_on_page:
                safe_lower_bound = 0
                if pushed_ids:
                    sorted_pushed = sorted(pushed_ids, reverse=True)
                    window_size = max_pages * limit
                    safe_lower_bound = sorted_pushed[min(len(sorted_pushed) - 1, window_size - 1)]

                page_min_id = min(page_ids) if page_ids else 0
                if page_min_id <= safe_lower_bound:
                    log.info("%s: caught up after %d pages (track: %s)", username, page, label)
                    break
                else:
                    log.info(
                        "%s: page %d has no new items, but min_id %d > safe_lower_bound %d. "
                        "Continuing to search for potential holes.",
                        username, page, page_min_id, safe_lower_bound,
                    )

            log.info("%s: +%d new (page %d, track: %s)", username, len(new_on_page), page, label)

            if all_seen:
                union = seen_ids | all_seen
                if len(union) > len(seen_ids):
                    save_seen_ids(seen_dir, tg_id, username, union)

            if not next_cursor:
                break
            cursor = next_cursor

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
    """Walk every page of the user's gallery. Propagates FetchPageError."""
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

    if max_total_gb > 0:
        max_bytes = max_total_gb * 1024 * 1024 * 1024
        all_files = []
        for root, dirs, files in os.walk(output_dir):
            for fname in files:
                fpath = Path(root) / fname
                try:
                    st = fpath.stat()
                    all_files.append((st.st_mtime, st.st_size, fpath))
                except OSError:
                    pass
        total_size = sum(f[1] for f in all_files)
        if total_size > max_bytes:
            all_files.sort()
            for _, size, fpath in all_files:
                if total_size <= max_bytes:
                    break
                try:
                    fpath.unlink()
                    removed += 1
                    total_size -= size
                except OSError:
                    pass

    return removed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

_monitor_shutdown_requested = False


def _monitor_signal_handler(signum, _frame):
    global _monitor_shutdown_requested
    if _monitor_shutdown_requested:
        return
    _monitor_shutdown_requested = True
    log.info("monitor.py: signal %d received, finishing current page then exiting", signum)


signal.signal(signal.SIGTERM, _monitor_signal_handler)
signal.signal(signal.SIGINT, _monitor_signal_handler)


def _acquire_process_lock() -> int | None:
    """Acquire the .monitor.lock file with an exclusive fcntl lock."""
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
    """Clear running status. On interrupt, leave an interrupted snapshot on disk."""
    try:
        if interrupted:
            STATUS_PATH.write_text(json.dumps({
                "status": "interrupted",
                "elapsed_seconds": int((_dt.datetime.now() - start_time).total_seconds()),
            }))
            return  # keep interrupted snapshot for operators
        if STATUS_PATH.exists():
            STATUS_PATH.unlink()
    except OSError:
        pass


def _build_backfill_summary(username: str, new_count: int, mode: str, nsfw: str, video_enabled: bool) -> str:
    """Compose the Telegram message that announces a backfill completion."""
    safe_user = escape_markdown(username)
    return (
        f"✅ *Backfill complete for @{safe_user}*\n"
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
    """Run a single creator through incremental or full mode."""
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
    log.info(
        "Merged %d new IDs for @%s (TG:%s) (total: %d)",
        new_count, username, tg_id_str, len(union),
    )
    return union, new_count


def main() -> None:
    # Step 1: process lock.
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
    set_tg_api_base(cfg.telegram.api_base_url)
    if args.mode:
        cfg.mode = args.mode

    log.info(
        "Mode: %s | NSFW: %s | Video: %s | Incremental max_pages: %d",
        cfg.mode, cfg.nsfw, cfg.video_enabled, cfg.incremental.max_pages,
    )

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
    exit_code = 0
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
                if cfg.mode == "full" and new_count > 0:
                    log.info(
                        "Full backfill complete for @%s (TG:%s): %d new items",
                        username, tg_id_str, new_count,
                    )
                    send_to_telegram(
                        cfg.telegram.bot_token, cfg.telegram.chat_id,
                        _build_backfill_summary(
                            username, new_count, cfg.mode, cfg.nsfw, cfg.video_enabled,
                        ),
                    )
    except FetchPageError as e:
        log.error("Fatal page fetch failure: %s", e)
        exit_code = 2
    finally:
        _clear_status(_monitor_shutdown_requested, start_time)

    if exit_code:
        sys.exit(exit_code)

    # Step 5: cache cleanup. Skipped if we were interrupted — exit fast instead.
    if not _monitor_shutdown_requested:
        removed = cleanup_old_caches(
            output_dir, cfg.download.keep_days, cfg.download.max_total_gb,
        )
        if removed:
            log.info(
                "Cleaned %d cached files older than %d days",
                removed, cfg.download.keep_days,
            )


if __name__ == "__main__":
    main()
