"""Telegram media/text send helpers with Markdown escape and 429 retry."""

from __future__ import annotations

import json
import logging
import random
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


def _telegram_post(
    url: str,
    *,
    timeout: float | int,
    max_retries: int = _TG_MAX_RETRIES,
    **kwargs,
) -> requests.Response:
    """POST to Telegram with limited retries for 429 Retry-After + short backoff.

    Timeouts are re-raised immediately (caller treats them as uncertain delivery).
    """
    last_resp: requests.Response | None = None
    last_exc: Exception | None = None
    for attempt in range(max_retries):
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
                    wait, attempt + 1, max_retries, e,
                )
                time.sleep(wait)
                continue
            raise
    if last_resp is not None:
        return last_resp
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("telegram post: exhausted retries without response")


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
            is_video = any(fp.suffix.lower() in (".mp4", ".webm", ".mov") for fp in valid_files)

            if is_video:
                return _send_telegram_video(api_base, chat_id, text, valid_files[0])

            return _send_telegram_media_group(api_base, chat_id, text, valid_files)

    return _send_telegram_text(api_base, chat_id, text)


def _send_telegram_video(api_base: str, chat_id: str, text: str, video_path: Path) -> bool | None:
    """Send a video to Telegram. <=50 MB: sendVideo (inline play). >50 MB: sendDocument.

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
            resp = _telegram_post(
                endpoint,
                data={"chat_id": chat_id, "caption": text, "parse_mode": "Markdown"},
                files={field_name: (video_path.name, f, "video/mp4")},
                timeout=300,
            )
        if resp.ok:
            return True
        log.warning("Video send failed: %s", resp.text[:200])
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

        resp = _telegram_post(
            f"{api_base}/sendMediaGroup",
            data={"chat_id": chat_id, "media": json.dumps(media)},
            files=files,
            timeout=60,
        )
        if resp.ok:
            return True
        log.warning("Media group send failed: %s", resp.text[:200])
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
        resp = _telegram_post(
            f"{api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        return resp.ok
    except requests.RequestException as e:
        log.error("Message send failed: %s", e)
        return False
