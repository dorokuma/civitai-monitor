"""Telegram message budget helpers, upload wrappers, and push bookkeeping."""

from __future__ import annotations

import logging
from pathlib import Path
from types import ModuleType

from civitai_monitor._compat import mod_attr
from media_compress import CompressSettings
from state_store import (
    StateWriteError,
)
from state_store import (
    record_push_history as _default_record_push_history,
)
from state_store import (
    rollback_push_slot as _default_rollback_push_slot,
)
from telegram_media import (
    SendOutcome,
    send_media_to_telegram_outcome,
    send_to_telegram,
)

log = logging.getLogger("civitai-monitor")

TELEGRAM_TEXT_BUDGET = 4000
TELEGRAM_CAPTION_BUDGET = 1000
CREATED_AT_BUDGET = 200

_ORIGINAL_SEND_TO_TELEGRAM = send_to_telegram


def _tm_send_detailed(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None,
    compress: CompressSettings | None = None,
) -> tuple[bool, bool]:
    """Call telegram_media.send_to_telegram_detailed (hardened contract)."""
    import telegram_media as _tm

    fn = getattr(_tm, "send_to_telegram_detailed", None)
    if fn is None:
        raise ImportError(
            "telegram_media.send_to_telegram_detailed is missing; update "
            "telegram_media.py to the hardened contract"
        )
    return fn(bot_token, chat_id, text, file_paths, compress)


def send_to_telegram_detailed(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None,
    compress: CompressSettings | None = None,
    *,
    mod: ModuleType | None = None,
) -> tuple[bool, bool]:
    """Legacy (delivered, media_failed) contract — kept for older callers."""
    send_fn = mod_attr(mod, "monitor", "send_to_telegram", _ORIGINAL_SEND_TO_TELEGRAM)
    orig_fn = mod_attr(mod, "monitor", "_ORIGINAL_SEND_TO_TELEGRAM", _ORIGINAL_SEND_TO_TELEGRAM)
    if send_fn is not orig_fn:
        return send_fn(bot_token, chat_id, text, file_paths), False
    return _tm_send_detailed(bot_token, chat_id, text, file_paths, compress)


_ORIGINAL_DETAILED_WRAPPER = send_to_telegram_detailed


def send_media_outcome(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None,
    compress: CompressSettings | None = None,
    *,
    prepared=None,
    mod: ModuleType | None = None,
) -> SendOutcome:
    """Single-send entry point returning the strict SendOutcome classification."""
    send_fn = mod_attr(mod, "monitor", "send_to_telegram", _ORIGINAL_SEND_TO_TELEGRAM)
    orig_send = mod_attr(mod, "monitor", "_ORIGINAL_SEND_TO_TELEGRAM", _ORIGINAL_SEND_TO_TELEGRAM)
    if send_fn is not orig_send:
        delivered = send_fn(bot_token, chat_id, text, file_paths)
        return SendOutcome.DELIVERED_OK if delivered else SendOutcome.DEFINITELY_NOT_SENT

    detailed_fn = mod_attr(
        mod, "monitor", "send_to_telegram_detailed", send_to_telegram_detailed,
    )
    orig_detailed = mod_attr(
        mod, "monitor", "_ORIGINAL_DETAILED_WRAPPER", _ORIGINAL_DETAILED_WRAPPER,
    )
    if detailed_fn is not orig_detailed:
        delivered, media_failed = detailed_fn(
            bot_token, chat_id, text, file_paths, compress,
        )
        if delivered:
            return (
                SendOutcome.REJECTED_PERMANENT
                if media_failed else SendOutcome.DELIVERED_OK
            )
        return SendOutcome.DEFINITELY_NOT_SENT
    return send_media_to_telegram_outcome(
        bot_token, chat_id, text, file_paths, compress, prepared=prepared,
    )


def _telegram_text_units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units (what the Bot API counts)."""
    return len(text.encode("utf-16-le")) // 2


def _clip(text: str, limit: int) -> str:
    """Clip ``text`` to at most ``limit`` Telegram characters (UTF-16 units)."""
    if limit < 1:
        return ""
    if _telegram_text_units(text) <= limit:
        return text
    kept: list[str] = []
    used = 0
    for ch in text:
        width = 2 if ord(ch) > 0xFFFF else 1
        if used + width > limit - 1:
            break
        kept.append(ch)
        used += width
    return "".join(kept).rstrip("\\") + "…"


def _drop_empty_files(files: list[Path] | None) -> list[Path] | None:
    """Return ``files`` minus any 0-byte files; ``None`` if nothing remains."""
    if not files:
        return files
    kept: list[Path] = []
    for fp in files:
        try:
            if fp.exists() and fp.stat().st_size == 0:
                log.warning(
                    "Dropping 0-byte media file (Telegram cannot upload it): %s",
                    fp.name,
                )
                continue
        except OSError:
            pass
        kept.append(fp)
    return kept or None


def _record_push_success(
    item_id: int,
    username: str,
    *,
    pushed_ids: set[int] | None,
    pushed_dir: Path | None,
    tg_id: str | None,
    mod: ModuleType | None = None,
) -> None:
    """Bookkeep a confirmed DELIVERED_OK push under the claim-slot contract."""
    if pushed_ids is None or pushed_dir is None or tg_id is None:
        return
    pushed_ids.add(item_id)
    record_fn = mod_attr(mod, "monitor", "record_push_history", _default_record_push_history)
    try:
        record_fn(pushed_dir, tg_id, username, item_id)
    except (StateWriteError, OSError) as e:
        log.warning("Could not record push history for @%s: %s", username, e)


def _finalize_send_outcome(
    outcome: bool | None,
    item_id: int,
    username: str,
    *,
    pushed_ids: set[int] | None,
    pushed_dir: Path | None,
    tg_id: str | None,
    chat_id: str | None = None,
    mod: ModuleType | None = None,
) -> bool:
    """Apply strict at-most-once bookkeeping for a legacy bool|None outcome."""
    if outcome is True:
        _record_push_success(
            item_id, username,
            pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id=tg_id, mod=mod,
        )
        return True
    if outcome is False:
        if pushed_dir is not None and tg_id is not None:
            rollback_fn = mod_attr(mod, "monitor", "rollback_push_slot", _default_rollback_push_slot)
            try:
                rollback_fn(
                    pushed_dir, chat_id or "", tg_id, username, item_id,
                )
            except StateWriteError as e:
                log.warning(
                    "Could not roll back push slot for id=%d @%s: %s",
                    item_id, username, e,
                )
        if pushed_ids is not None:
            pushed_ids.discard(item_id)
        return False
    if pushed_ids is not None:
        pushed_ids.add(item_id)
    log.warning(
        "id=%d for @%s ended UNCERTAIN (timeout/5xx/disconnect); claim kept, "
        "no resend — manual check recommended",
        item_id, username,
    )
    return False
