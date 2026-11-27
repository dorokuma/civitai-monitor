#!/usr/bin/env python3

"""
Civitai Monitor — Enhanced Edition

Monitors specified Civitai users for new image & video uploads,
downloads full-resolution originals, and pushes them to a Telegram
channel via the Bot API.

Orchestration facade: domain logic lives in ``civitai_monitor/``, while this
module preserves CLI entry and backward-compatible symbols (`from monitor import X`).
"""

from __future__ import annotations

import datetime as _dt
import fcntl  # noqa: F401
import logging
import os  # noqa: F401
import signal
import sys
import time  # noqa: F401
from collections.abc import Callable
from pathlib import Path
from typing import Any

import load_env  # noqa: F401  # 自动加载同目录下的 civitai-bot.env（token 等敏感信息）
from civitai_client import (
    HTTP_REQUEST_TIMEOUT,  # noqa: F401
    MAX_API_PAGE_LIMIT,  # noqa: F401
    MIN_API_PAGE_LIMIT,  # noqa: F401
    FetchPageError,  # noqa: F401
    RateLimitError,  # noqa: F401
    fetch_page,  # noqa: F401
    init_session,  # noqa: F401
    safe_get,  # noqa: F401
    session,  # noqa: F401
)
from civitai_monitor import media_download as _media_dl
from civitai_monitor import monitor_pipeline as _pipeline
from civitai_monitor import monitor_runner as _runner
from civitai_monitor import push_utils as _push
from civitai_monitor.media_download import (
    IMAGE_DOWNLOAD_TIMEOUT,  # noqa: F401
    IMAGE_EXT_WHITELIST,  # noqa: F401
    MAX_IMAGE_DOWNLOAD_MB,  # noqa: F401
    VIDEO_DOWNLOAD_TIMEOUT,  # noqa: F401
    VIDEO_EXT_WHITELIST,  # noqa: F401
    DownloadResult,
    _coerce_item_id,  # noqa: F401
    _is_path_within,  # noqa: F401
    _is_permanent_request_error,  # noqa: F401
    _VideoSizeCapExceeded,  # noqa: F401
    normalize_to_original,  # noqa: F401
    nsfw_tracks,  # noqa: F401
    track_label,  # noqa: F401
)
from civitai_monitor.monitor_pipeline import (
    MAX_CONSECUTIVE_EMPTY_PAGES,  # noqa: F401
    MAX_FULL_PAGES_PER_TRACK,  # noqa: F401
    cleanup_old_caches,  # noqa: F401
)
from civitai_monitor.monitor_runner import (
    _build_backfill_summary,  # noqa: F401
    _release_process_lock,  # noqa: F401
)
from civitai_monitor.push_utils import (
    CREATED_AT_BUDGET,  # noqa: F401
    TELEGRAM_CAPTION_BUDGET,  # noqa: F401
    TELEGRAM_TEXT_BUDGET,  # noqa: F401
    _clip,  # noqa: F401
    _drop_empty_files,  # noqa: F401
    _telegram_text_units,  # noqa: F401
    _tm_send_detailed,  # noqa: F401
)
from config_io import (
    DATA_DIR_NAME,  # noqa: F401
    DEFAULT_CONFIG_PATHS,  # noqa: F401
    LOCK_FILE_NAME,
    SCRIPT_DIR,
    STATUS_FILE_NAME,
    VALID_MODES,  # noqa: F401
    VALID_NSFW,  # noqa: F401
    ApiConfig,  # noqa: F401
    BackfillConfig,  # noqa: F401
    CompressConfig,  # noqa: F401
    DataConfig,  # noqa: F401
    DownloadConfig,  # noqa: F401
    HttpConfig,  # noqa: F401
    IncrementalConfig,  # noqa: F401
    MonitorConfig,
    ReconciliationConfig,  # noqa: F401
    TelegramConfig,  # noqa: F401
    load_config,  # noqa: F401
    redact_config_for_disk,  # noqa: F401
    write_config,  # noqa: F401
)
from media_compress import CompressSettings
from state_store import (
    PENDING_CONFIRM_SECONDS,  # noqa: F401
    PENDING_MAX_RETRIES,  # noqa: F401
    PendingMap,  # noqa: F401
    StateReadError,  # noqa: F401
    StateWriteError,  # noqa: F401
    absorb_legacy_push_states,  # noqa: F401
    adopt_stale_inflight,  # noqa: F401
    claim_backlog_truncation_alert,  # noqa: F401
    claim_push_slot,  # noqa: F401
    clear_inflight,  # noqa: F401
    clear_pending,  # noqa: F401
    load_chat_pushed_ids,  # noqa: F401
    load_pending_map,  # noqa: F401
    load_push_timestamps,  # noqa: F401
    load_pushed_ids,  # noqa: F401
    load_seen_ids,  # noqa: F401
    mark_inflight,  # noqa: F401
    mark_pending,  # noqa: F401
    pushed_file_for_user,  # noqa: F401
    record_push_history,  # noqa: F401
    rollback_backlog_truncation_alert,  # noqa: F401
    rollback_push_slot,  # noqa: F401
    save_pushed_ids,  # noqa: F401
    save_seen_ids,  # noqa: F401
    seen_file_for_user,  # noqa: F401
    update_pending_map,  # noqa: F401
    update_push_timestamps,  # noqa: F401
)
from telegram_media import (
    TELEGRAM_PHOTO_MAX_BYTES,  # noqa: F401
    TELEGRAM_VIDEO_MAX_MB,  # noqa: F401
    SendOutcome,
    _cleanup_compressed_artifact,  # noqa: F401
    _send_telegram_media_group,  # noqa: F401
    _send_telegram_text,  # noqa: F401
    _send_telegram_text_outcome,  # noqa: F401
    _send_telegram_video,  # noqa: F401
    escape_markdown,  # noqa: F401
    get_tg_api_base,  # noqa: F401
    prepare_media_for_upload,  # noqa: F401
    send_media_to_telegram_outcome,  # noqa: F401
    send_to_telegram,
    set_tg_api_base,  # noqa: F401
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)
log = logging.getLogger("civitai-monitor")
logging.getLogger("httpx").setLevel(logging.WARNING)

_ORIGINAL_SEND_TO_TELEGRAM = send_to_telegram
LOCK_PATH = SCRIPT_DIR / LOCK_FILE_NAME
STATUS_PATH = SCRIPT_DIR / STATUS_FILE_NAME
_monitor_shutdown_requested = False


def _self() -> Any:
    return sys.modules[__name__]


def send_to_telegram_detailed(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None,
    compress: CompressSettings | None = None,
) -> tuple[bool, bool]:
    return _push.send_to_telegram_detailed(
        bot_token, chat_id, text, file_paths, compress, mod=_self(),
    )


_ORIGINAL_DETAILED_WRAPPER = send_to_telegram_detailed


def send_media_outcome(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None,
    compress: CompressSettings | None = None,
    *,
    prepared=None,
) -> SendOutcome:
    return _push.send_media_outcome(
        bot_token, chat_id, text, file_paths, compress, prepared=prepared, mod=_self(),
    )


def download_image(url: str, save_path: Path, timeout: int = 120) -> DownloadResult:
    return _media_dl.download_image(url, save_path, timeout=timeout, mod=_self())


def download_video(url: str, save_path: Path, max_size_mb: int = 1024) -> DownloadResult:
    return _media_dl.download_video(url, save_path, max_size_mb=max_size_mb, mod=_self())


def _record_push_success(
    item_id: int,
    username: str,
    *,
    pushed_ids: set[int] | None,
    pushed_dir: Path | None,
    tg_id: str | None,
) -> None:
    _push._record_push_success(
        item_id, username,
        pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id, mod=_self(),
    )


def _finalize_send_outcome(
    outcome: bool | None,
    item_id: int,
    username: str,
    *,
    pushed_ids: set[int] | None,
    pushed_dir: Path | None,
    tg_id: str | None,
    chat_id: str | None = None,
) -> bool:
    return _push._finalize_send_outcome(
        outcome, item_id, username,
        pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id,
        chat_id=chat_id, mod=_self(),
    )


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
    compress: CompressSettings | None = None,
    pushed_ids: set[int] | None = None,
    pushed_dir: Path | None = None,
    tg_id: str | None = None,
    shutdown_flag: Callable[[], bool] = lambda: False,
) -> bool:
    return _pipeline.process_and_push(
        item, username,
        size_suffixes=size_suffixes, output_dir=output_dir,
        bot_token=bot_token, chat_id=chat_id,
        video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
        compress=compress, pushed_ids=pushed_ids, pushed_dir=pushed_dir,
        tg_id=tg_id, shutdown_flag=shutdown_flag, mod=_self(),
    )


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
    compress: CompressSettings | None = None,
    shutdown_flag: Callable[[], bool] = lambda: False,
) -> tuple[list[dict], set[int], str]:
    return _pipeline._fetch_and_process_page(
        username, nsfw_flag, cursor, seen_ids, pushed_ids,
        base_url=base_url, limit=limit, sort=sort,
        size_suffixes=size_suffixes, output_dir=output_dir,
        bot_token=bot_token, chat_id=chat_id,
        video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
        pushed_dir=pushed_dir, tg_id=tg_id, compress=compress,
        shutdown_flag=shutdown_flag, mod=_self(),
    )


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
    compress: CompressSettings | None = None,
    max_pages: int = 50,
    hole_window_items: int = 500,
    shutdown_flag: Callable[[], bool] = lambda: False,
) -> set[int]:
    return _pipeline.run_incremental(
        username,
        seen_ids=seen_ids, tg_id=tg_id, seen_dir=seen_dir,
        nsfw_setting=nsfw_setting, output_dir=output_dir,
        size_suffixes=size_suffixes, bot_token=bot_token, chat_id=chat_id,
        base_url=base_url, limit=limit, video_enabled=video_enabled,
        max_video_size_mb=max_video_size_mb, compress=compress,
        max_pages=max_pages, hole_window_items=hole_window_items,
        shutdown_flag=shutdown_flag, mod=_self(),
    )


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
    compress: CompressSettings | None = None,
    seen_dir: Path,
    shutdown_flag: Callable[[], bool] = lambda: False,
) -> set[int]:
    return _pipeline.run_full(
        username,
        seen_ids=seen_ids, tg_id=tg_id, nsfw_setting=nsfw_setting,
        output_dir=output_dir, size_suffixes=size_suffixes,
        bot_token=bot_token, chat_id=chat_id, base_url=base_url,
        limit=limit, video_enabled=video_enabled,
        max_video_size_mb=max_video_size_mb, compress=compress,
        seen_dir=seen_dir, shutdown_flag=shutdown_flag, mod=_self(),
    )


def run_reconciliation(
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
    compress: CompressSettings | None = None,
    max_pages_per_track: int = 200,
    max_consecutive_no_new_pages: int = 3,
    shutdown_flag: Callable[[], bool] = lambda: False,
) -> tuple[set[int], int, int]:
    return _pipeline.run_reconciliation(
        username,
        seen_ids=seen_ids, tg_id=tg_id, seen_dir=seen_dir,
        nsfw_setting=nsfw_setting, output_dir=output_dir,
        size_suffixes=size_suffixes, bot_token=bot_token, chat_id=chat_id,
        base_url=base_url, limit=limit, video_enabled=video_enabled,
        max_video_size_mb=max_video_size_mb, compress=compress,
        max_pages_per_track=max_pages_per_track,
        max_consecutive_no_new_pages=max_consecutive_no_new_pages,
        shutdown_flag=shutdown_flag, mod=_self(),
    )


def _monitor_signal_handler(signum, _frame):
    global _monitor_shutdown_requested
    if _monitor_shutdown_requested:
        return
    _monitor_shutdown_requested = True
    log.info("monitor.py: signal %d received, finishing current page then exiting", signum)


signal.signal(signal.SIGTERM, _monitor_signal_handler)
signal.signal(signal.SIGINT, _monitor_signal_handler)


def _acquire_process_lock() -> int | None:
    return _runner._acquire_process_lock(mod=_self())


def _write_status(
    *,
    start_time: _dt.datetime,
    mode: str,
    current_creator: str,
    creators_done: int,
    creators_total: int,
    pushed_count: int,
) -> None:
    _runner._write_status(
        start_time=start_time, mode=mode, current_creator=current_creator,
        creators_done=creators_done, creators_total=creators_total,
        pushed_count=pushed_count, mod=_self(),
    )


def _clear_status(interrupted: bool, start_time: _dt.datetime) -> None:
    _runner._clear_status(interrupted, start_time, mod=_self())


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
) -> tuple[set[int], int]:
    return _runner._process_single_creator(
        username, tg_id_str, seen_dir, output_dir,
        mode=mode, nsfw=nsfw, size_suffixes=size_suffixes,
        bot_token=bot_token, chat_id=chat_id, base_url=base_url,
        page_limit=page_limit, video_enabled=video_enabled,
        max_video_size_mb=max_video_size_mb, compress=compress,
        incremental_max_pages=incremental_max_pages,
        hole_window_items=hole_window_items,
        reconcile_max_pages=reconcile_max_pages,
        reconcile_consecutive_no_new=reconcile_consecutive_no_new,
        mod=_self(),
    )


def _process_creator_queue(
    subs: dict[str, Any],
    cfg: MonitorConfig,
    *,
    seen_dir: Path,
    output_dir: Path,
    user_filter: str,
    start_time: _dt.datetime,
) -> tuple[int, bool]:
    return _runner._process_creator_queue(
        subs, cfg, seen_dir=seen_dir, output_dir=output_dir,
        user_filter=user_filter, start_time=start_time, mod=_self(),
    )


def main() -> None:
    _runner.run_monitor_main(mod=_self())


if __name__ == "__main__":
    main()
