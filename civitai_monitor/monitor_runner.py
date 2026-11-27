"""Process lock, status snapshots, creator queue, and CLI entrypoint."""

from __future__ import annotations

import argparse
import atexit
import datetime as _dt
import fcntl
import json
import logging
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

from civitai_client import FetchPageError
from civitai_monitor._compat import mod_attr
from civitai_monitor.monitor_pipeline import (
    cleanup_old_caches as _default_cleanup_old_caches,
)
from civitai_monitor.monitor_pipeline import (
    run_full as _default_run_full,
)
from civitai_monitor.monitor_pipeline import (
    run_incremental as _default_run_incremental,
)
from civitai_monitor.monitor_pipeline import (
    run_reconciliation as _default_run_reconciliation,
)
from config_io import (
    DATA_DIR_NAME,
    LOCK_FILE_NAME,
    SCRIPT_DIR,
    STATUS_FILE_NAME,
    MonitorConfig,
)
from config_io import (
    load_config as _default_load_config,
)
from media_compress import CompressSettings
from state_store import (
    StateWriteError,
)
from state_store import (
    load_seen_ids as _default_load_seen_ids,
)
from state_store import (
    save_seen_ids as _default_save_seen_ids,
)
from telegram_media import (
    TELEGRAM_PHOTO_MAX_BYTES,
    TELEGRAM_VIDEO_MAX_MB,
    escape_markdown,
)
from telegram_media import (
    send_to_telegram as _default_send_to_telegram,
)
from telegram_media import (
    set_tg_api_base as _default_set_tg_api_base,
)

log = logging.getLogger("civitai-monitor")


def _release_process_lock(lock_fd: int, lock_file: Path) -> None:
    """Release the monitor process lock by closing the fd."""
    try:
        os.close(lock_fd)
    except OSError:
        pass


def _acquire_process_lock(*, mod: ModuleType | None = None) -> int | None:
    """Acquire the .monitor.lock file with an exclusive fcntl lock."""
    script_dir = mod_attr(mod, "monitor", "SCRIPT_DIR", SCRIPT_DIR)
    lock_file = script_dir / LOCK_FILE_NAME
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

    atexit.register(_release_process_lock, lock_fd, lock_file)
    return lock_fd


def _write_status(
    *,
    start_time: _dt.datetime,
    mode: str,
    current_creator: str,
    creators_done: int,
    creators_total: int,
    pushed_count: int,
    mod: ModuleType | None = None,
) -> None:
    """Write a single status snapshot for /status consumers to read."""
    status_path = mod_attr(mod, "monitor", "STATUS_PATH", SCRIPT_DIR / STATUS_FILE_NAME)
    payload = {
        "status": "running",
        "started_at": start_time.strftime("%H:%M:%S"),
        "mode": mode,
        "current_creator": current_creator,
        "creators_done": creators_done,
        "creators_total": creators_total,
        "pushed_count": pushed_count,
        "elapsed_seconds": int((_dt.datetime.now(_dt.timezone.utc) - start_time).total_seconds()),
    }
    tmp_path = status_path.with_name(status_path.name + ".tmp")
    try:
        with open(tmp_path, "w") as f:
            f.write(json.dumps(payload))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, status_path)
    except OSError as e:
        log.warning("Failed to write monitor status file (best effort): %s", e)
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _clear_status(
    interrupted: bool,
    start_time: _dt.datetime,
    *,
    mod: ModuleType | None = None,
) -> None:
    """Clear running status. On interrupt, leave an interrupted snapshot on disk."""
    status_path = mod_attr(mod, "monitor", "STATUS_PATH", SCRIPT_DIR / STATUS_FILE_NAME)
    try:
        if interrupted:
            status_path.write_text(json.dumps({
                "status": "interrupted",
                "elapsed_seconds": int((_dt.datetime.now(_dt.timezone.utc) - start_time).total_seconds()),
            }))
            return
        if status_path.exists():
            status_path.unlink()
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
    compress: CompressSettings | None = None,
    incremental_max_pages: int,
    hole_window_items: int = 500,
    reconcile_max_pages: int = 200,
    reconcile_consecutive_no_new: int = 3,
    mod: ModuleType | None = None,
) -> tuple[set[int], int]:
    """Run a single creator through incremental, full, or reconcile mode."""
    load_seen_fn = mod_attr(mod, "monitor", "load_seen_ids", _default_load_seen_ids)
    save_seen_fn = mod_attr(mod, "monitor", "save_seen_ids", _default_save_seen_ids)
    run_full_fn = mod_attr(mod, "monitor", "run_full", _default_run_full)
    run_reconcile_fn = mod_attr(mod, "monitor", "run_reconciliation", _default_run_reconciliation)
    run_inc_fn = mod_attr(mod, "monitor", "run_incremental", _default_run_incremental)

    def _shutdown_flag() -> bool:
        return bool(mod_attr(mod, "monitor", "_monitor_shutdown_requested", False))

    seen_ids = load_seen_fn(seen_dir, tg_id_str, username)
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
        "compress": compress,
    }
    try:
        if mode == "full":
            user_seen = run_full_fn(**common, shutdown_flag=_shutdown_flag)
        elif mode == "reconcile":
            user_seen, _new_pushed, _pages = run_reconcile_fn(
                **common,
                max_pages_per_track=reconcile_max_pages,
                max_consecutive_no_new_pages=reconcile_consecutive_no_new,
                shutdown_flag=_shutdown_flag,
            )
        else:
            user_seen = run_inc_fn(
                **common,
                max_pages=incremental_max_pages,
                hole_window_items=hole_window_items,
                shutdown_flag=_shutdown_flag,
            )

        if not user_seen:
            return seen_ids, 0
        union = seen_ids | user_seen
        if len(union) <= len(seen_ids):
            return seen_ids, 0
        save_seen_fn(seen_dir, tg_id_str, username, union)
    except StateWriteError:
        log.error(
            "State persistence failed for @%s (TG:%s); skipping this creator. "
            "In-flight items will be recovered on the next scan.",
            username, tg_id_str,
        )
        return seen_ids, 0
    new_count = len(union) - len(seen_ids)
    log.info(
        "Merged %d new IDs for @%s (TG:%s) (total: %d)",
        new_count, username, tg_id_str, len(union),
    )
    return union, new_count


def _process_creator_queue(
    subs: dict[str, Any],
    cfg: MonitorConfig,
    *,
    seen_dir: Path,
    output_dir: Path,
    user_filter: str,
    start_time: _dt.datetime,
    mod: ModuleType | None = None,
) -> tuple[int, bool]:
    """Walk every (TG user, creator) pair and process each one."""
    write_status_fn = mod_attr(mod, "monitor", "_write_status", _write_status)
    process_creator_fn = mod_attr(mod, "monitor", "_process_single_creator", _process_single_creator)
    send_tg_fn = mod_attr(mod, "monitor", "send_to_telegram", _default_send_to_telegram)
    build_summary_fn = mod_attr(mod, "monitor", "_build_backfill_summary", _build_backfill_summary)

    compress_settings = replace(
        CompressSettings.from_config(getattr(cfg, "compress", None)),
        photo_target_bytes=TELEGRAM_PHOTO_MAX_BYTES,
        video_target_bytes=TELEGRAM_VIDEO_MAX_MB * 1024 * 1024,
    )
    log.info(
        "Compression: enabled=%s | photo<=%d MB | video<=%d MB | timeout=%ds",
        compress_settings.enabled,
        compress_settings.photo_target_bytes // 1048576,
        compress_settings.video_target_bytes // 1048576,
        compress_settings.timeout_seconds,
    )

    total_creators = sum(len(v) for v in subs.values())
    pushed_count = 0
    creator_idx = 0
    had_fetch_error = False
    for tg_id, user_list in subs.items():
        if mod_attr(mod, "monitor", "_monitor_shutdown_requested", False):
            log.info("monitor.py: shutdown requested, stopping user loop")
            break
        tg_id_str = str(tg_id)
        for entry in user_list:
            if mod_attr(mod, "monitor", "_monitor_shutdown_requested", False):
                log.info("monitor.py: shutdown requested, stopping creator loop")
                break
            username = entry.get("name", str(entry)) if isinstance(entry, dict) else str(entry)
            if user_filter and username != user_filter:
                log.info("Skipping @%s (--user filter active)", username)
                continue
            creator_idx += 1
            write_status_fn(
                start_time=start_time, mode=cfg.mode, current_creator=username,
                creators_done=creator_idx, creators_total=total_creators,
                pushed_count=pushed_count,
            )
            log.info("=" * 50)
            log.info("Processing @%s (TG:%s, %s mode)...", username, tg_id_str, cfg.mode)
            try:
                _seen_after, new_count = process_creator_fn(
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
                    hole_window_items=getattr(cfg.incremental, "hole_window_items", 500),
                    reconcile_max_pages=cfg.reconciliation.max_pages_per_track,
                    reconcile_consecutive_no_new=cfg.reconciliation.max_consecutive_no_new_pages,
                    compress=compress_settings,
                )
            except FetchPageError as e:
                had_fetch_error = True
                log.error(
                    "Page fetch failed for @%s (TG:%s): %s; continuing with next creator",
                    username, tg_id_str, e,
                )
                continue
            except Exception:
                log.exception(
                    "Unhandled error while processing @%s (TG:%s); "
                    "skipping to the next creator",
                    username, tg_id_str,
                )
                continue
            pushed_count += new_count
            if cfg.mode == "full" and new_count > 0:
                log.info(
                    "Full backfill complete for @%s (TG:%s): %d new items",
                    username, tg_id_str, new_count,
                )
                send_tg_fn(
                    cfg.telegram.bot_token, cfg.telegram.chat_id,
                    build_summary_fn(
                        username, new_count, cfg.mode, cfg.nsfw, cfg.video_enabled,
                    ),
                )
    return pushed_count, had_fetch_error


def run_monitor_main(*, mod: ModuleType | None = None) -> None:
    acquire_lock_fn = mod_attr(mod, "monitor", "_acquire_process_lock", _acquire_process_lock)
    load_config_fn = mod_attr(mod, "monitor", "load_config", _default_load_config)
    set_base_fn = mod_attr(mod, "monitor", "set_tg_api_base", _default_set_tg_api_base)
    script_dir = mod_attr(mod, "monitor", "SCRIPT_DIR", SCRIPT_DIR)
    write_status_fn = mod_attr(mod, "monitor", "_write_status", _write_status)
    clear_status_fn = mod_attr(mod, "monitor", "_clear_status", _clear_status)
    queue_fn = mod_attr(mod, "monitor", "_process_creator_queue", _process_creator_queue)
    cleanup_fn = mod_attr(mod, "monitor", "cleanup_old_caches", _default_cleanup_old_caches)

    if acquire_lock_fn() is None:
        log.warning("Another monitor process is already running - skipping this cron tick")
        sys.exit(75)

    parser = argparse.ArgumentParser(description="Civitai Monitor — civitai.com user gallery monitor")
    parser.add_argument("--config", type=str, help="Path to config.yaml (default: auto-search)")
    parser.add_argument("--mode", type=str, choices=["incremental", "full", "reconcile"], help="Override scan mode")
    parser.add_argument("--user", type=str, help="Process only this Civitai username")
    args = parser.parse_args()

    cfg = load_config_fn(Path(args.config) if args.config else None)
    if mod is not None:
        mod.cfg = cfg
    if cfg is None:
        sys.exit(1)
    set_base_fn(cfg.telegram.api_base_url)
    if args.mode:
        cfg.mode = args.mode

    log.info(
        "Mode: %s | NSFW: %s | Video: %s | Incremental max_pages: %d",
        cfg.mode, cfg.nsfw, cfg.video_enabled, cfg.incremental.max_pages,
    )

    data_dir = Path(cfg.data.data_dir) if cfg.data.data_dir else script_dir
    output_dir = data_dir / cfg.download.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    seen_dir = data_dir / DATA_DIR_NAME
    seen_dir.mkdir(parents=True, exist_ok=True)

    subs = cfg.subscriptions or {}
    if not subs:
        log.error("No subscriptions configured in config.yaml")
        sys.exit(1)

    start_time = _dt.datetime.now(_dt.timezone.utc)
    total_creators = sum(len(v) for v in subs.values())
    write_status_fn(
        start_time=start_time, mode=cfg.mode, current_creator="",
        creators_done=0, creators_total=total_creators, pushed_count=0,
    )

    exit_code = 0
    try:
        _pushed_count, had_fetch_error = queue_fn(
            subs, cfg,
            seen_dir=seen_dir, output_dir=output_dir,
            user_filter=args.user, start_time=start_time,
        )
        if had_fetch_error:
            exit_code = 2
    except FetchPageError as e:
        log.error("Fatal page fetch failure: %s", e)
        exit_code = 2
    finally:
        interrupted = bool(mod_attr(mod, "monitor", "_monitor_shutdown_requested", False))
        clear_status_fn(interrupted, start_time)

    if exit_code:
        sys.exit(exit_code)

    if not mod_attr(mod, "monitor", "_monitor_shutdown_requested", False):
        removed = cleanup_fn(
            output_dir, cfg.download.keep_days, cfg.download.max_total_gb,
        )
        if removed:
            log.info(
                "Cleaned %d cached files older than %d days",
                removed, cfg.download.keep_days,
            )
