"""Telegram media/text send helpers with Markdown escape and 429 retry."""

from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path

import requests

log = logging.getLogger("civitai-monitor")

# Telegram send limits
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
TELEGRAM_DOCUMENT_MAX_BYTES = 50 * 1024 * 1024  # 50 MB; larger is rejected
# 本地 telegram-bot-api 服务器（127.0.0.1:8081）的文件上限是 2GB（2000MB），
# sendVideo 在本地服务器下同样放宽到 2GB，所以这里的 2048MB 阈值是匹配的，不是 bug。
# 不要把它改成 50——那是官方云 API api.telegram.org 的 sendVideo 上限。
# 本项目永远使用自建本地服务器（不会切回官方 API），此值保持 2048。
TELEGRAM_VIDEO_MAX_MB = 2048

_tg_api_base = "https://api.telegram.org"

# Limited retries for transient Telegram API pressure (429 etc.)
_TG_MAX_RETRIES = 4
_TG_SHORT_RETRY_BASE = 1.0

# requests.RequestException strings embed the full request URL
# (https://api.telegram.org/bot<token>/...), so logging an exception verbatim
# leaks the bot token into the log file.  Mask every "/bot<token>" segment
# before it reaches a log call.
_BOT_TOKEN_URL_RE = re.compile(r"/bot[^/\s]+")


def _sanitize_text(text: str) -> str:
    """Mask "/bot<token>" URL segments (-> "/bot***"); other text is untouched."""
    return _BOT_TOKEN_URL_RE.sub("/bot***", str(text))


def _sanitize_exc(exc: BaseException) -> str:
    """Stringify an exception for logging with any bot token masked out."""
    return _sanitize_text(str(exc))


def set_tg_api_base(url: str) -> None:
    """Configure Bot API base URL (e.g. local telegram-bot-api server)."""
    global _tg_api_base
    _tg_api_base = url.rstrip("/") if url else "https://api.telegram.org"


def get_tg_api_base() -> str:
    return _tg_api_base


def escape_markdown(text: str) -> str:
    """Escape Telegram *legacy* Markdown special characters in dynamic text.

    Dynamic fields (usernames with ``_``, etc.) break ``parse_mode=Markdown``
    unless escaped. Order: backslash first, then other specials.
    """
    if not text:
        return text
    out = str(text)
    for ch in ("\\", "_", "*", "`", "["):
        out = out.replace(ch, "\\" + ch)
    return out


def _rewind_upload_files(files: dict | None) -> None:
    """seek(0) every file object inside a requests ``files`` mapping.

    Values may be bare file objects or ``(name, fileobj, mime)`` tuples.
    Retry attempts must re-read the payload from the start: an already
    consumed handle uploads 0 bytes, Telegram answers 400, and the media
    would be permanently lost to the text-only fallback.
    """
    if not files:
        return
    for value in files.values():
        candidates: tuple = value if isinstance(value, (tuple, list)) else (value,)
        for obj in candidates:
            seek = getattr(obj, "seek", None)
            if callable(seek):
                try:
                    obj.seek(0)
                except ValueError as e:
                    # A closed handle raises ValueError ("seek of closed
                    # file"), not OSError. Left bare it would escape the retry
                    # loop in _telegram_post and hit the per-item catch;
                    # mapping it onto OSError funnels it into the existing
                    # rewind guard there, which converts it to
                    # requests.RequestException like any other rewind failure.
                    raise OSError(f"cannot rewind file handle: {e}") from e


def _telegram_post(
    url: str,
    *,
    timeout: float,
    max_retries: int = _TG_MAX_RETRIES,
    **kwargs,
) -> requests.Response:
    """POST to Telegram with limited retries for 429 Retry-After + short backoff.

    Timeouts are re-raised immediately (caller treats them as uncertain delivery).
    File objects in ``files`` are rewound (seek(0)) before every attempt so a
    retry re-uploads the full payload instead of 0 bytes.
    """
    last_resp: requests.Response | None = None
    last_exc: Exception | None = None
    for attempt in range(max_retries):
        # Rewind upload handles before every attempt: requests consumes the
        # file objects in `files`, so an un-rewound retry would upload 0 bytes
        # and Telegram would answer 400 (media lost to the text fallback).
        try:
            _rewind_upload_files(kwargs.get("files"))
        except OSError as e:
            log.warning("Telegram upload aborted: cannot rewind file handle: %s", e)
            raise requests.RequestException(f"upload file rewind failed: {e}") from e
        try:
            resp = requests.post(url, timeout=timeout, **kwargs)
            last_resp = resp
            if resp.status_code == 429:
                retry_after_raw = resp.headers.get("Retry-After", "3")
                try:
                    retry_after = int(float(retry_after_raw))
                except (TypeError, ValueError):
                    retry_after = 3
                wait = max(1, retry_after) + random.uniform(0, 1)
                log.warning(
                    "Telegram 429 Retry-After=%ss, sleeping %.1fs (attempt %d/%d)",
                    retry_after, wait, attempt + 1, max_retries,
                )
                if attempt < max_retries - 1:
                    time.sleep(wait)
                    continue
                return resp
            # Short retry on 5xx
            if 500 <= resp.status_code < 600 and attempt < max_retries - 1:
                wait = _TG_SHORT_RETRY_BASE * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning(
                    "Telegram %d, short retry in %.1fs (attempt %d/%d)",
                    resp.status_code, wait, attempt + 1, max_retries,
                )
                time.sleep(wait)
                continue
            return resp
        except requests.Timeout:
            raise
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                wait = _TG_SHORT_RETRY_BASE * (2 ** attempt) + random.uniform(0, 0.5)
                log.warning(
                    "Telegram transport error, short retry in %.1fs (attempt %d/%d): %s",
                    wait, attempt + 1, max_retries, _sanitize_exc(e),
                )
                time.sleep(wait)
                continue
            raise
    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("telegram post: exhausted retries without response")


def send_to_telegram_detailed(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None = None,
) -> tuple[bool, bool]:
    """Send a Telegram message and report the media outcome separately.

    Returns (delivered, media_failed):
      delivered    — the user-visible push is confirmed: media delivered, or
                     media failed but the text fallback was delivered.
      media_failed — True only when ``file_paths`` were provided with existing
                     files and the media upload ultimately failed (0-byte
                     upload, HTTP 400, 429 exhausted, transport error, or an
                     uncertain timeout). A pure-text send never sets it.

    Timeout policy: after an uncertain transport timeout the text fallback
    is NOT sent (the media may already be in the chat) and the outcome is
    (False, True). The caller must not mark the item pushed; the monitor
    re-sends the whole item on a later scan, which can duplicate an
    already-delivered media — accepted over silently dropping it.
    """
    api_base = f"{_tg_api_base}/bot{bot_token}"

    if file_paths:
        valid_files = [fp for fp in file_paths if fp.exists()]
        if valid_files:
            is_video = any(fp.suffix.lower() in (".mp4", ".webm", ".mov") for fp in valid_files)

            if is_video:
                ok, media_failed = _send_telegram_video_with_status(api_base, chat_id, text, valid_files[0])
            else:
                ok, media_failed = _send_telegram_media_group_with_status(api_base, chat_id, text, valid_files)
            return ok is True, media_failed

    return _send_telegram_text(api_base, chat_id, text), False


def send_to_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None = None,
) -> bool:
    """Send a Telegram message (thin legacy wrapper).

    Returns True when the user-visible push is confirmed, False otherwise
    (confirmed failure, or uncertain timeout delivery — the historical None
    "uncertain" marker is folded into False). See
    :func:`send_to_telegram_detailed` for the separate media outcome.
    """
    ok, _ = send_to_telegram_detailed(bot_token, chat_id, text, file_paths)
    return ok


def _send_telegram_video_with_status(
    api_base: str, chat_id: str, text: str, video_path: Path
) -> tuple[bool | None, bool]:
    """Send a video, reporting (ok, media_failed) for send_to_telegram_detailed.

    ``ok`` keeps the legacy three-state meaning of ``_send_telegram_video``:
      * HTTP success → True
      * Clear HTTP error → text-only fallback result (media was rejected; safe
        to notify) — True when the fallback text was delivered
      * Timeout after the request left the client → None (uncertain; no text
        fallback — the video may already be in the chat)
      * Other transport errors → False without text (retry media next scan)

    ``media_failed`` is True whenever the media upload did not succeed
    (HTTP error, transport error, or uncertain timeout).

    Caller view: send_to_telegram_detailed folds ``ok`` via ``ok is True``,
    so a timeout surfaces to the monitor as (False, True) — not pushed, and
    the whole item is re-sent on a later scan. A video that was in fact
    delivered before the timeout may therefore be pushed twice; that
    trade-off is accepted over silently losing the media.
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
            resp = _telegram_post(
                endpoint,
                data={"chat_id": chat_id, "caption": text, "parse_mode": "Markdown"},
                files={field_name: (video_path.name, f, "video/mp4")},
                timeout=300,
            )
        if resp.ok:
            return True, False
        log.warning("Video send failed: %s", resp.text[:200])
        return _send_telegram_text(api_base, chat_id, text), True
    except requests.Timeout as e:
        log.warning(
            "Video send timeout (may already be delivered); marking uncertain, no text fallback: %s",
            _sanitize_exc(e),
        )
        return None, True
    except requests.RequestException as e:
        log.warning("Video send error (will retry later, no text fallback): %s", _sanitize_exc(e))
        return False, True


def _send_telegram_video(api_base: str, chat_id: str, text: str, video_path: Path) -> bool | None:
    """Legacy thin wrapper: video send outcome only (see *_with_status)."""
    ok, _ = _send_telegram_video_with_status(api_base, chat_id, text, video_path)
    return ok


def _send_telegram_media_group_with_status(
    api_base: str, chat_id: str, text: str, file_paths: list[Path]
) -> tuple[bool | None, bool]:
    """Send images as a media group, reporting (ok, media_failed).

    ``ok`` keeps the legacy three-state meaning of
    ``_send_telegram_media_group``. Same timeout policy as
    ``_send_telegram_video_with_status``: never append a text message after a
    transport timeout (the photo may already be in the chat); that outcome is
    (None, True) here and folds to (False, True) in send_to_telegram_detailed
    — the monitor re-sends the whole item on a later scan, so a group that
    was in fact delivered before the timeout may be pushed twice (accepted
    over silently losing the media).
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
                fh = open(fp, "rb")  # noqa: SIM115
                open_handles.append(fh)
                files[f"img{i}"] = (fp.name, fh, "image/jpeg")

        if not media:
            return False, True

        resp = _telegram_post(
            f"{api_base}/sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media)},
            files=files,
            timeout=60,
        )
        if resp.ok:
            return True, False
        log.warning("Media group send failed: %s", resp.text[:200])
        return _send_telegram_text(api_base, chat_id, text), True
    except requests.Timeout as e:
        log.warning(
            "Media group timeout (may already be delivered); marking uncertain, no text fallback: %s",
            _sanitize_exc(e),
        )
        return None, True
    except requests.RequestException as e:
        log.warning("Media group error (will retry later, no text fallback): %s", _sanitize_exc(e))
        return False, True
    finally:
        for fh in open_handles:
            fh.close()


def _send_telegram_media_group(api_base: str, chat_id: str, text: str, file_paths: list[Path]) -> bool | None:
    """Legacy thin wrapper: media group outcome only (see *_with_status)."""
    ok, _ = _send_telegram_media_group_with_status(api_base, chat_id, text, file_paths)
    return ok


def _send_telegram_text(api_base: str, chat_id: str, text: str) -> bool:
    try:
        resp = _telegram_post(
            f"{api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        return resp.ok
    except requests.RequestException as e:
        log.error("Message send failed: %s", _sanitize_exc(e))
        return False
