"""Scan, backfill, reconciliation, push pipeline, and cache cleanup."""

from __future__ import annotations

import logging
import os
import random
import time as _time_mod
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

from civitai_client import fetch_page as _default_fetch_page
from civitai_monitor._compat import mod_attr
from civitai_monitor.media_download import (
    IMAGE_EXT_WHITELIST,
    VIDEO_EXT_WHITELIST,
    _coerce_item_id,
    _is_path_within,
    normalize_to_original,
    nsfw_tracks,
    track_label,
)
from civitai_monitor.media_download import (
    download_image as _default_download_image,
)
from civitai_monitor.media_download import (
    download_video as _default_download_video,
)
from civitai_monitor.push_utils import (
    CREATED_AT_BUDGET,
    TELEGRAM_CAPTION_BUDGET,
    TELEGRAM_TEXT_BUDGET,
    _clip,
    _drop_empty_files,
)
from civitai_monitor.push_utils import (
    _record_push_success as _default_record_push_success,
)
from civitai_monitor.push_utils import (
    send_media_outcome as _default_send_media_outcome,
)
from media_compress import CompressSettings
from state_store import (
    StateReadError,
    StateWriteError,
)
from state_store import (
    absorb_legacy_push_states as _default_absorb_legacy_push_states,
)
from state_store import (
    claim_backlog_truncation_alert as _default_claim_backlog_truncation_alert,
)
from state_store import (
    claim_push_slot as _default_claim_push_slot,
)
from state_store import (
    load_chat_pushed_ids as _default_load_chat_pushed_ids,
)
from state_store import (
    load_pushed_ids as _default_load_pushed_ids,
)
from state_store import (
    rollback_backlog_truncation_alert as _default_rollback_backlog_truncation_alert,
)
from state_store import (
    rollback_push_slot as _default_rollback_push_slot,
)
from state_store import (
    save_seen_ids as _default_save_seen_ids,
)
from telegram_media import (
    SendOutcome,
    _cleanup_compressed_artifact,
    escape_markdown,
    get_tg_api_base,
    prepare_media_for_upload,
)
from telegram_media import (
    _send_telegram_text_outcome as _default_send_telegram_text_outcome,
)

log = logging.getLogger("civitai-monitor")

MAX_FULL_PAGES_PER_TRACK = 500
MAX_CONSECUTIVE_EMPTY_PAGES = 3


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
    mod: ModuleType | None = None,
) -> bool:
    """Download a single item (image or video) and push to Telegram — strict at-most-once."""
    raw_id = item.get("id")
    item_id = _coerce_item_id(raw_id)
    if item_id is None:
        log.warning(
            "Dropping item with malformed id %r for @%s (id must be an int)",
            raw_id, username,
        )
        return False
    civitai_url = f"https://civitai.com/images/{item_id}"
    created_at = _clip(escape_markdown(item.get("createdAt", "")), CREATED_AT_BUDGET)
    safe_user = escape_markdown(username)

    is_video = (
        video_enabled
        and (
            item.get("type") == "video"
            or str(item.get("url", "")).lower().endswith(tuple(VIDEO_EXT_WHITELIST))
        )
    )

    stateful = pushed_dir is not None and tg_id is not None

    drop_empty_fn = mod_attr(mod, "monitor", "_drop_empty_files", _drop_empty_files)
    clip_fn = mod_attr(mod, "monitor", "_clip", _clip)
    claim_slot_fn = mod_attr(mod, "monitor", "claim_push_slot", _default_claim_push_slot)
    rollback_slot_fn = mod_attr(mod, "monitor", "rollback_push_slot", _default_rollback_push_slot)
    send_outcome_fn = mod_attr(mod, "monitor", "send_media_outcome", _default_send_media_outcome)
    record_success_fn = mod_attr(mod, "monitor", "_record_push_success", _default_record_push_success)
    prepare_fn = mod_attr(mod, "monitor", "prepare_media_for_upload", prepare_media_for_upload)
    cleanup_art_fn = mod_attr(mod, "monitor", "_cleanup_compressed_artifact", _cleanup_compressed_artifact)
    dl_video_fn = mod_attr(mod, "monitor", "download_video", _default_download_video)
    dl_image_fn = mod_attr(mod, "monitor", "download_image", _default_download_image)

    def _send(text: str, files: list[Path] | None) -> bool:
        files = drop_empty_fn(files)
        text = clip_fn(text, TELEGRAM_CAPTION_BUDGET if files else TELEGRAM_TEXT_BUDGET)
        prepared = None
        if files:
            try:
                prepared = prepare_fn(files, compress)
            except Exception:
                log.exception(
                    "Media preparation crashed for id=%d @%s; skipping item "
                    "this scan (no claim taken)",
                    item_id, username,
                )
                return False
        try:
            claimed = False
            if stateful:
                if pushed_ids is not None and item_id in pushed_ids:
                    return False
                try:
                    claimed = claim_slot_fn(
                        pushed_dir, chat_id, tg_id, username, item_id,
                    )
                except StateReadError:
                    raise
                except StateWriteError as e:
                    log.error(
                        "Cannot claim push slot for id=%d @%s (%s); "
                        "skipping with NO send (fail-closed)",
                        item_id, username, e,
                    )
                    return False
                if not claimed:
                    if pushed_ids is not None:
                        pushed_ids.add(item_id)
                    log.info(
                        "id=%d for @%s already claimed for chat %s; "
                        "skipping send", item_id, username, chat_id,
                    )
                    return False
            if shutdown_flag():
                if claimed:
                    try:
                        rollback_slot_fn(
                            pushed_dir, chat_id, tg_id, username, item_id,
                        )
                    except StateWriteError as e:
                        log.error(
                            "Could not release push slot for id=%d @%s on "
                            "shutdown: %s", item_id, username, e,
                        )
                return False

            outcome = send_outcome_fn(
                bot_token, chat_id, text, files, compress, prepared=prepared,
            )
        finally:
            if prepared is not None:
                for artifact in prepared.artifacts:
                    cleanup_art_fn(artifact)

        if outcome is SendOutcome.DELIVERED_OK:
            record_success_fn(
                item_id, username,
                pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id,
            )
            return True
        if outcome is SendOutcome.DEFINITELY_NOT_SENT:
            if claimed:
                try:
                    rollback_slot_fn(pushed_dir, chat_id, tg_id, username, item_id)
                except StateWriteError as e:
                    log.error(
                        "Rollback of push slot failed for id=%d @%s: %s",
                        item_id, username, e,
                    )
            if pushed_ids is not None:
                pushed_ids.discard(item_id)
            log.warning(
                "id=%d for @%s definitively not sent (connect/429); "
                "claim released, will retry next scan",
                item_id, username,
            )
            return False
        if pushed_ids is not None:
            pushed_ids.add(item_id)
        log.warning(
            "id=%d for @%s terminal outcome %s to chat %s — claim kept, "
            "NO rollback and NO resend. Manual check recommended.",
            item_id, username, outcome.value, chat_id,
        )
        return False

    if is_video:
        meta = item.get("meta")
        meta = meta if isinstance(meta, dict) else {}
        video_url = item.get("url") or meta.get("videoUrl", "")
        if not video_url:
            log.warning("Video %s: no URL found", item_id)
            return False

        filepath = output_dir / "videos" / f"{item_id}.mp4"
        if not _is_path_within(filepath, output_dir):
            log.warning(
                "Dropping video id=%d for @%s: resolved path %s escapes output dir %s",
                item_id, username, filepath, output_dir,
            )
            return False
        dl = dl_video_fn(video_url, filepath, max_video_size_mb)

        if dl.success:
            text = (
                f"🎥 *New video by @{safe_user}* ✅\n"
                f"🔗 [View on Civitai]({civitai_url})\n"
                f"🕐 {created_at}"
            )
            pushed = _send(text, [filepath])
        elif dl.permanent:
            text = (
                f"🎥 *New video by @{safe_user}* ⚠️（视频下载失败，请在 Civitai 页面查看）\n"
                f"🔗 [View on Civitai]({civitai_url})\n"
                f"🕐 {created_at}"
            )
            pushed = _send(text, None)
        else:
            log.warning(
                "Transient video download failure for id=%d @%s; skipping "
                "Telegram notice, will retry next scan",
                item_id, username,
            )
            return False
        log.info(
            "Pushed video %s to @%s | id=%d file=%s success=%s push=%s",
            "✅" if pushed else "❌", username, item_id,
            filepath.name if dl.success else "none", dl.success, pushed,
        )
        return pushed

    raw_url = item.get("url")
    if not isinstance(raw_url, str) or not raw_url:
        log.warning("Image %s: no usable URL, skipping", item_id)
        return False
    orig_url = normalize_to_original(raw_url, size_suffixes)
    ext = os.path.splitext(orig_url.split("/")[-1])[1].lower() or ".jpeg"
    if ext not in IMAGE_EXT_WHITELIST:
        log.warning(
            "Dropping image id=%d for @%s: URL extension %r not in whitelist",
            item_id, username, ext,
        )
        return False
    filepath = output_dir / f"{item_id}{ext}"
    if not _is_path_within(filepath, output_dir):
        log.warning(
            "Dropping image id=%d for @%s: resolved path %s escapes output dir %s",
            item_id, username, filepath, output_dir,
        )
        return False
    dl = dl_image_fn(orig_url, filepath)

    if dl.success:
        text = (
            f"🖼 *New artwork by @{safe_user}* ✅\n"
            f"🔗 [View on Civitai]({civitai_url})\n"
            f"🕐 {created_at}"
        )
        pushed = _send(text, [filepath])
    elif dl.permanent:
        text = (
            f"🖼 *New artwork by @{safe_user}* ⚠️（原图下载失败，请在 Civitai 页面查看）\n"
            f"🔗 [View on Civitai]({civitai_url})\n"
            f"🕐 {created_at}"
        )
        pushed = _send(text, None)
    else:
        log.warning(
            "Transient image download failure for id=%d @%s; skipping "
            "Telegram notice, will retry next scan",
            item_id, username,
        )
        return False
    log.info(
        "Pushed image %s to @%s | id=%d file=%s success=%s push=%s",
        "✅" if pushed else "❌", username, item_id,
        filepath.name if dl.success else "none", dl.success, pushed,
    )
    return pushed


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
    mod: ModuleType | None = None,
) -> tuple[list[dict], set[int], str]:
    """Fetch one page (by cursor), find new items, process and push them."""
    fetch_page_fn = mod_attr(mod, "monitor", "fetch_page", _default_fetch_page)
    absorb_fn = mod_attr(mod, "monitor", "absorb_legacy_push_states", _default_absorb_legacy_push_states)
    load_chat_fn = mod_attr(mod, "monitor", "load_chat_pushed_ids", _default_load_chat_pushed_ids)
    push_fn = mod_attr(mod, "monitor", "process_and_push", process_and_push)
    time_mod = mod_attr(mod, "monitor", "time", _time_mod)

    items, next_cursor = fetch_page_fn(
        username, base_url=base_url, limit=limit, cursor=cursor, nsfw=nsfw_flag, sort=sort,
    )
    if not items:
        return [], set(), next_cursor

    page_ids: set[int] = set()
    valid_items: list[dict[str, Any]] = []
    for img in items:
        raw_id = img.get("id")
        item_id = _coerce_item_id(raw_id)
        if item_id is None:
            log.warning(
                "Dropping item with malformed id %r from @%s page (id must be an int)",
                raw_id, username,
            )
            continue
        if not isinstance(raw_id, int):
            img = {**img, "id": item_id}
        page_ids.add(item_id)
        valid_items.append(img)

    absorbed = absorb_fn(pushed_dir, chat_id, tg_id, username)
    chat_pushed = load_chat_fn(pushed_dir, chat_id)
    pushed_ids |= absorbed | (page_ids & chat_pushed)
    effective_pushed = pushed_ids | chat_pushed

    new_on_page = [img for img in valid_items if img["id"] not in effective_pushed]
    did_push_attempt = False

    if new_on_page:
        processed_this_page: set[int] = set()
        for img in reversed(new_on_page):
            item_id = img["id"]
            if isinstance(item_id, bool) or not isinstance(item_id, int):
                log.warning(
                    "Skipping item with non-int id %r for @%s (path safety)",
                    item_id, username,
                )
                continue
            if item_id in processed_this_page or item_id in effective_pushed:
                continue
            if shutdown_flag():
                log.info(
                    "Shutdown requested; stopping item loop for @%s at id=%d",
                    username, item_id,
                )
                break
            processed_this_page.add(item_id)

            try:
                did_push_attempt = True
                push_fn(
                    img, username,
                    size_suffixes=size_suffixes,
                    output_dir=output_dir,
                    bot_token=bot_token,
                    chat_id=chat_id,
                    video_enabled=video_enabled,
                    max_video_size_mb=max_video_size_mb,
                    compress=compress,
                    pushed_ids=pushed_ids,
                    pushed_dir=pushed_dir,
                    tg_id=tg_id,
                    shutdown_flag=shutdown_flag,
                )
                time_mod.sleep(2.0 + random.random() * 1.0)
            except StateReadError:
                raise
            except Exception:
                log.exception(
                    "Unhandled error while processing id=%s for @%s; skipping item",
                    item_id, username,
                )
                continue

    if not did_push_attempt:
        return [], page_ids, next_cursor
    return new_on_page, page_ids, next_cursor


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
    mod: ModuleType | None = None,
) -> set[int]:
    """Check latest content using cursor pagination. Propagates FetchPageError."""
    load_pushed_fn = mod_attr(mod, "monitor", "load_pushed_ids", _default_load_pushed_ids)
    fetch_proc_fn = mod_attr(mod, "monitor", "_fetch_and_process_page", _fetch_and_process_page)
    save_seen_fn = mod_attr(mod, "monitor", "save_seen_ids", _default_save_seen_ids)
    claim_alert_fn = mod_attr(mod, "monitor", "claim_backlog_truncation_alert", _default_claim_backlog_truncation_alert)
    rollback_alert_fn = mod_attr(mod, "monitor", "rollback_backlog_truncation_alert", _default_rollback_backlog_truncation_alert)
    send_text_fn = mod_attr(mod, "monitor", "_send_telegram_text_outcome", _default_send_telegram_text_outcome)
    get_tg_base_fn = mod_attr(mod, "monitor", "get_tg_api_base", get_tg_api_base)
    time_mod = mod_attr(mod, "monitor", "time", _time_mod)

    all_seen: set[int] = set(seen_ids)
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = track_label(nsfw_flag)
        cursor = ""
        page = 0
        visited_cursors: set[str] = set()
        last_new_count = 0
        while page < max_pages:
            if shutdown_flag():
                log.info("%s: shutdown requested, stopping incremental at page %d", label, page)
                break
            page += 1
            visited_cursors.add(cursor)
            pushed_ids = load_pushed_fn(seen_dir, tg_id, username)
            new_on_page, page_ids, next_cursor = fetch_proc_fn(
                username, nsfw_flag, cursor, all_seen, pushed_ids,
                base_url=base_url, limit=limit, sort="Newest",
                size_suffixes=size_suffixes, output_dir=output_dir,
                bot_token=bot_token, chat_id=chat_id,
                video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
                pushed_dir=seen_dir, tg_id=tg_id, compress=compress,
                shutdown_flag=shutdown_flag,
            )
            last_new_count = len(new_on_page)

            if next_cursor and next_cursor in visited_cursors:
                log.warning(
                    "%s: cursor %r already visited at page %d, stopping incremental (API cursor loop)",
                    label, next_cursor, page,
                )
                break

            if not page_ids:
                if next_cursor:
                    log.info(
                        "%s: page %d empty but nextCursor present, continuing (track: %s)",
                        username, page, label,
                    )
                    cursor = next_cursor
                    time_mod.sleep(2.0 + random.random() * 1.0)
                    continue
                break

            all_seen.update(page_ids)
            if not new_on_page:
                safe_lower_bound = 0
                if pushed_ids:
                    sorted_pushed = sorted(pushed_ids, reverse=True)
                    window_size = hole_window_items
                    safe_lower_bound = sorted_pushed[min(len(sorted_pushed) - 1, max(0, window_size - 1))]

                page_min_id = min(page_ids) if page_ids else 0
                if page_min_id <= safe_lower_bound:
                    log.info("%s: caught up after %d pages (track: %s)", username, page, label)
                    break
                else:
                    log.info(
                        "%s: page %d has no new items, but min_id %d > safe_lower_bound %d. "
                        "Continuing to search for potential holes (track: %s).",
                        username, page, page_min_id, safe_lower_bound, label,
                    )

            log.info("%s: +%d new (page %d, track: %s)", username, len(new_on_page), page, label)

            if all_seen:
                union = seen_ids | all_seen
                if len(union) > len(seen_ids):
                    save_seen_fn(seen_dir, tg_id, username, union)

            if not next_cursor:
                break
            cursor = next_cursor
            time_mod.sleep(2.0 + random.random() * 1.0)
        else:
            if page >= max_pages and last_new_count > 0:
                alert_msg = f"@{username} 积压超过单次扫描容量 (track: {label})，请 /backfill"
                log.warning("%s (page=%d, max_pages=%d)", alert_msg, page, max_pages)
                try:
                    if bot_token and chat_id and claim_alert_fn(seen_dir, username, label):
                        alert_outcome = None
                        try:
                            alert_outcome = send_text_fn(
                                f"{get_tg_base_fn()}/bot{bot_token}",
                                chat_id, alert_msg,
                            )
                        except Exception as e:  # noqa: BLE001
                            log.warning("Failed to send backlog truncation alert to Telegram: %s", e)
                        if alert_outcome in (
                            SendOutcome.DEFINITELY_NOT_SENT, None,
                        ):
                            rollback_alert_fn(seen_dir, username, label)
                except Exception as e:  # noqa: BLE001
                    log.warning("Failed to send backlog truncation alert to Telegram: %s", e)

        if all_seen:
            union = seen_ids | all_seen
            if len(union) > len(seen_ids):
                save_seen_fn(seen_dir, tg_id, username, union)

    return all_seen


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
    mod: ModuleType | None = None,
) -> set[int]:
    """Walk every page of the user's gallery. Propagates FetchPageError."""
    max_pages_cap = mod_attr(mod, "monitor", "MAX_FULL_PAGES_PER_TRACK", MAX_FULL_PAGES_PER_TRACK)
    max_empty_cap = mod_attr(mod, "monitor", "MAX_CONSECUTIVE_EMPTY_PAGES", MAX_CONSECUTIVE_EMPTY_PAGES)
    load_pushed_fn = mod_attr(mod, "monitor", "load_pushed_ids", _default_load_pushed_ids)
    fetch_proc_fn = mod_attr(mod, "monitor", "_fetch_and_process_page", _fetch_and_process_page)
    save_seen_fn = mod_attr(mod, "monitor", "save_seen_ids", _default_save_seen_ids)
    time_mod = mod_attr(mod, "monitor", "time", _time_mod)

    all_seen: set[int] = set(seen_ids)
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = track_label(nsfw_flag)
        log.info("── %s track for @%s ──", label, username)
        cursor = ""
        page = 0
        consecutive_empty_pages = 0
        visited_cursors: set[str] = set()

        while True:
            if shutdown_flag():
                log.info("%s: shutdown requested, stopping at page %d", label, page)
                break
            page += 1
            if page > max_pages_cap:
                log.warning(
                    "%s: reached hard page cap of %d pages, stopping backfill "
                    "(possible unguarded loop)", label, max_pages_cap,
                )
                break
            pushed_ids = load_pushed_fn(seen_dir, tg_id, username)
            new_on_page, page_ids, next_cursor = fetch_proc_fn(
                username, nsfw_flag, cursor, all_seen, pushed_ids,
                base_url=base_url, limit=limit,
                size_suffixes=size_suffixes, output_dir=output_dir,
                bot_token=bot_token, chat_id=chat_id,
                video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
                pushed_dir=seen_dir, tg_id=tg_id, compress=compress,
                shutdown_flag=shutdown_flag,
            )

            visited_cursors.add(cursor)
            if next_cursor and next_cursor in visited_cursors:
                log.warning(
                    "%s: cursor %r already visited at page %d, stopping backfill "
                    "(API cursor loop)", label, next_cursor, page,
                )
                break

            if not page_ids:
                if next_cursor:
                    consecutive_empty_pages += 1
                    log.info(
                        "%s: page %d empty but nextCursor present, continuing "
                        "(consecutive empty: %d)", label, page, consecutive_empty_pages,
                    )
                    if consecutive_empty_pages >= max_empty_cap:
                        log.warning(
                            "%s: %d consecutive empty pages, stopping backfill",
                            label, consecutive_empty_pages,
                        )
                        break
                    cursor = next_cursor
                    time_mod.sleep(2.0 + random.random() * 1.0)
                    continue
                log.info("%s: exhausted after %d pages", label, page - 1)
                break

            consecutive_empty_pages = 0
            all_seen.update(page_ids)

            if new_on_page:
                log.info("%s page %d: +%d new", label, page, len(new_on_page))
            else:
                log.info("%s page %d: all %d already seen", label, page, len(page_ids))

            save_seen_fn(seen_dir, tg_id, username, all_seen)

            if not next_cursor:
                log.info("%s: completed after %d pages", label, page)
                break
            cursor = next_cursor
            time_mod.sleep(2.0 + random.random() * 1.0)

    return all_seen


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
    mod: ModuleType | None = None,
) -> tuple[set[int], int, int]:
    """Deep reconciliation scan for a creator across all configured tracks."""
    load_pushed_fn = mod_attr(mod, "monitor", "load_pushed_ids", _default_load_pushed_ids)
    fetch_proc_fn = mod_attr(mod, "monitor", "_fetch_and_process_page", _fetch_and_process_page)
    save_seen_fn = mod_attr(mod, "monitor", "save_seen_ids", _default_save_seen_ids)
    time_mod = mod_attr(mod, "monitor", "time", _time_mod)

    all_seen: set[int] = set(seen_ids)
    tracks = nsfw_tracks(nsfw_setting)
    total_new_pushed = 0
    total_pages_walked = 0

    for nsfw_flag in tracks:
        label = track_label(nsfw_flag)
        log.info("── [RECONCILE] %s track for @%s ──", label, username)
        cursor = ""
        page = 0
        consecutive_no_new = 0
        visited_cursors: set[str] = set()

        while True:
            if shutdown_flag():
                log.info("[RECONCILE] %s: shutdown requested, stopping at page %d", label, page)
                break
            page += 1
            total_pages_walked += 1
            if page > max_pages_per_track:
                log.warning(
                    "[RECONCILE] %s: reached max pages cap (%d), stopping track",
                    label, max_pages_per_track,
                )
                break

            pushed_ids = load_pushed_fn(seen_dir, tg_id, username)
            new_on_page, page_ids, next_cursor = fetch_proc_fn(
                username, nsfw_flag, cursor, all_seen, pushed_ids,
                base_url=base_url, limit=limit, sort="Newest",
                size_suffixes=size_suffixes, output_dir=output_dir,
                bot_token=bot_token, chat_id=chat_id,
                video_enabled=video_enabled, max_video_size_mb=max_video_size_mb,
                pushed_dir=seen_dir, tg_id=tg_id, compress=compress,
                shutdown_flag=shutdown_flag,
            )

            visited_cursors.add(cursor)
            if next_cursor and next_cursor in visited_cursors:
                log.warning(
                    "[RECONCILE] %s: cursor %r already visited at page %d, stopping track (API cursor loop)",
                    label, next_cursor, page,
                )
                break

            if not page_ids:
                if next_cursor:
                    consecutive_no_new += 1
                    log.info(
                        "[RECONCILE] %s: page %d empty but nextCursor present (consecutive no-new: %d)",
                        label, page, consecutive_no_new,
                    )
                    if consecutive_no_new >= max_consecutive_no_new_pages:
                        log.info(
                            "[RECONCILE] %s: reached %d consecutive empty/no-new pages, stopping track",
                            label, consecutive_no_new,
                        )
                        break
                    cursor = next_cursor
                    time_mod.sleep(2.0 + random.random() * 1.0)
                    continue
                log.info("[RECONCILE] %s: exhausted after %d pages", label, page - 1)
                break

            all_seen.update(page_ids)

            if new_on_page:
                consecutive_no_new = 0
                total_new_pushed += len(new_on_page)
                log.info("[RECONCILE] %s page %d: +%d new items found and pushed", label, page, len(new_on_page))
            else:
                consecutive_no_new += 1
                log.info(
                    "[RECONCILE] %s page %d: 0 new items (consecutive no-new: %d/%d)",
                    label, page, consecutive_no_new, max_consecutive_no_new_pages,
                )
                if consecutive_no_new >= max_consecutive_no_new_pages:
                    log.info(
                        "[RECONCILE] %s: reached %d consecutive pages with no new items, ending track at page %d",
                        label, consecutive_no_new, page,
                    )
                    break

            if all_seen:
                union = seen_ids | all_seen
                if len(union) > len(seen_ids):
                    save_seen_fn(seen_dir, tg_id, username, union)

            if not next_cursor:
                log.info("[RECONCILE] %s: gallery completed after %d pages", label, page)
                break
            cursor = next_cursor
            time_mod.sleep(2.0 + random.random() * 1.0)

        if all_seen:
            union = seen_ids | all_seen
            if len(union) > len(seen_ids):
                save_seen_fn(seen_dir, tg_id, username, union)

    log.info(
        "[RECONCILE RESULT] @%s (TG:%s): walked %d pages total across tracks, discovered and pushed %d new items",
        username, tg_id, total_pages_walked, total_new_pushed,
    )
    return all_seen, total_new_pushed, total_pages_walked


def cleanup_old_caches(output_dir: Path, keep_days: int, max_total_gb: int = 0) -> int:
    """按天数 + 按总大小双重清理。返回删除的文件数量。"""
    if not output_dir.exists():
        return 0

    if keep_days < 1:
        log.warning(
            "cleanup_old_caches: keep_days=%d < 1; refusing to delete anything",
            keep_days,
        )
        return 0

    removed = 0
    cutoff = _time_mod.time() - keep_days * 86400
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
        for root, _dirs, files in os.walk(output_dir):
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
