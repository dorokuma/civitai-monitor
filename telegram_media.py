"""Telegram media/text send helpers — strict at-most-once delivery contract.

Every send entry point returns a ``SendOutcome`` so the scheduler can apply
exactly one of four policies:

  DELIVERED_OK           — Telegram confirmed the push (HTTP 2xx). Keep the
                           claim; the item must never be sent again.
  DEFINITELY_NOT_SENT    — the request provably never left / was refused
                           before payload delivery (first-attempt connect
                           failure, or a 429 that survived the bounded
                           Retry-After wait with NO text notice sent). Safe
                           to roll the claim back; the item may retry later.
  REJECTED_PERMANENT     — Telegram answered a definitive 4xx, or the media
                           can never be uploaded locally (still oversized
                           after compression). At most ONE plain-text notice
                           may be attempted; whatever its result, the claim
                           is KEPT — retrying this item can only duplicate
                           or loop forever.
  UNCERTAIN_NO_ROLLBACK  — the request was sent but the outcome is unknown
                           (ReadTimeout, 5xx, RemoteDisconnected,
                           ConnectionResetError, ProtocolError, file-rewind
                           failure, ...). The media may already be in the
                           chat: NEVER send a text fallback and NEVER roll
                           the claim back.

Nothing in this module retries after the request has been sent:
_telegram_post retries only pre-flight 429s (bounded by
_TG_MAX_RETRY_AFTER) — everything else is returned/raised to the caller so
a "maybe delivered" message is never sent twice.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from enum import Enum
from pathlib import Path

import requests

import media_compress

log = logging.getLogger("civitai-monitor")

# Telegram send limits
TELEGRAM_PHOTO_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB; larger photos are compressed first
# 本地 telegram-bot-api 服务器（127.0.0.1:8081）的文件上限是 2GB（2000MB），
# sendVideo 在本地服务器下同样放宽到 2GB，所以这里的 2048MB 阈值是匹配的，不是 bug。
# 不要把它改成 50——那是官方云 API api.telegram.org 的 sendVideo 上限。
# 本项目永远使用自建本地服务器（不会切回官方 API），此值保持 2048。
# 超过此值的视频不再改走文档上传（该分支已整体移除）：先按 media_compress 的
# 无损优先阶梯压缩，压完仍超限才降级为纯文本通知。
TELEGRAM_VIDEO_MAX_MB = 2048

_tg_api_base = "https://api.telegram.org"

# Pre-flight 429 handling only: honor Retry-After but cap the total wait.
# A 429 whose Retry-After exceeds this returns immediately with its claim
# eligible for rollback (no payload was sent, so retrying is safe).
_TG_MAX_RETRIES = 4
_TG_MAX_RETRY_AFTER = 60  # seconds — never sleep longer than this per wait

# Per-endpoint (connect, read) timeouts. The connect half is short so a dead
# endpoint fails fast into DEFINITELY_NOT_SENT; the read half is generous
# because the local bot-api can take minutes to finish a large upload.
_TG_TIMEOUT_MEDIA_GROUP = (10.0, 180.0)
_TG_TIMEOUT_VIDEO = (10.0, 600.0)
_TG_TIMEOUT_MESSAGE = (10.0, 60.0)

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


class SendOutcome(Enum):
    """Terminal classification of a single Telegram send attempt."""

    DELIVERED_OK = "delivered_ok"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    REJECTED_PERMANENT = "rejected_permanent"
    UNCERTAIN_NO_ROLLBACK = "uncertain_no_rollback"


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
    consumed handle uploads 0 bytes and Telegram answers 400.
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
                    # file"), not OSError. Mapping it onto OSError funnels it
                    # into the rewind guard in _telegram_post, which converts
                    # it to requests.RequestException like any other rewind
                    # failure.
                    raise OSError(f"cannot rewind file handle: {e}") from e


def _telegram_post(
    url: str,
    *,
    timeout: float | tuple[float, float],
    max_retries: int = _TG_MAX_RETRIES,
    **kwargs,
) -> requests.Response:
    """POST to Telegram — retries ONLY bounded pre-flight HTTP 429s.

    Whitelist rationale: a 429 is returned before Telegram accepts the
    payload, so re-sending cannot duplicate the message. Every other
    non-2xx response, and every transport-level failure, is surfaced to the
    caller untouched — after the payload may have left the client, ANY
    automatic re-send is a potential duplicate.

    A 429 whose Retry-After exceeds ``_TG_MAX_RETRY_AFTER`` is returned
    immediately (no sleep): the caller classifies it DEFINITELY_NOT_SENT and
    rolls the claim back. File objects in ``files`` are rewound (seek(0))
    before every attempt.
    """
    for attempt in range(max_retries):
        try:
            _rewind_upload_files(kwargs.get("files"))
        except OSError as e:
            log.warning("Telegram upload aborted: cannot rewind file handle: %s", e)
            raise requests.RequestException(f"upload file rewind failed: {e}") from e
        resp = requests.post(url, timeout=timeout, **kwargs)
        if resp.status_code == 429 and attempt < max_retries - 1:
            retry_after_raw = resp.headers.get("Retry-After")
            if retry_after_raw is None:
                # Telegram Bot API (incl. local telegram-bot-api) also puts
                # the wait in the JSON body: parameters.retry_after — the
                # header is not always populated, so fall back to the body.
                try:
                    retry_after_raw = str(
                        resp.json().get("parameters", {}).get("retry_after", "3")
                    )
                except (ValueError, TypeError, AttributeError):
                    retry_after_raw = "3"
            try:
                retry_after = int(float(retry_after_raw))
            except (TypeError, ValueError, OverflowError):
                retry_after = 3
            if retry_after > _TG_MAX_RETRY_AFTER:
                # Wait too long: give up without sleeping. Nothing was sent,
                # so the claim may be rolled back safely.
                log.warning(
                    "Telegram 429 Retry-After=%ss exceeds %ss cap; not retrying",
                    retry_after, _TG_MAX_RETRY_AFTER,
                )
                return resp
            wait = max(1, retry_after) + random.uniform(0, 1)
            log.warning(
                "Telegram 429 Retry-After=%ss, sleeping %.1fs (attempt %d/%d)",
                retry_after, wait, attempt + 1, max_retries,
            )
            time.sleep(wait)
            continue
        return resp


def _request_definitely_not_sent(exc: requests.RequestException) -> bool:
    """True when ``exc`` proves the request never reached Telegram.

    First-attempt connect failures only: ConnectTimeout, and a ConnectionError
    whose underlying cause is urllib3's NewConnectionError — connection
    refused / no route, and its subclass NameResolutionError (DNS failure),
    which urllib3 2.x nests inside MaxRetryError.reason rather than the
    exception chain. Everything after a socket was established is
    uncertain: the payload may be in flight.
    """
    if isinstance(exc, requests.ConnectTimeout):
        return True
    if not isinstance(exc, requests.ConnectionError):
        return False
    try:
        from urllib3.exceptions import (
            MaxRetryError,
            NewConnectionError,
        )
    except ImportError:  # pragma: no cover - urllib3 is a requests dep
        MaxRetryError = NewConnectionError = ()  # type: ignore[assignment]

    def _is_connect_failure(err: BaseException | None, depth: int = 0) -> bool:
        if err is None or depth > 5:
            return False
        if isinstance(err, NewConnectionError):
            return True
        # urllib3 2.x: NewConnectionError/NameResolutionError live in
        # MaxRetryError.reason, not in exc.__cause__/__context__.
        if isinstance(err, MaxRetryError):
            return _is_connect_failure(err.reason, depth + 1)
        return (
            _is_connect_failure(err.__cause__, depth + 1)
            or _is_connect_failure(err.__context__, depth + 1)
        )

    if _is_connect_failure(exc):
        return True
    # String fallback: NewConnectionError / DNS failure names embedded in
    # the message when the chain could not be reconstructed.
    return bool(re.search(
        r"NewConnectionError|NameResolutionError|Failed to establish a new "
        r"connection|Failed to resolve",
        str(exc),
    ))


def send_media_to_telegram_outcome(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None = None,
    compress: media_compress.CompressSettings | None = None,
    *,
    prepared: PreparedMedia | None = None,
) -> SendOutcome:
    """Send one item to Telegram and classify the terminal outcome.

    Exactly one network attempt is made for the media (bounded pre-flight
    429s aside); a permanent rejection may be followed by at most one
    plain-text notice. Nothing else ever triggers a second request.

    ``prepared`` lets the caller run compression BEFORE claiming the push
    slot (see :func:`prepare_media_for_upload`); when omitted it is
    computed here from ``file_paths`` so direct callers keep working.
    """
    api_base = f"{_tg_api_base}/bot{bot_token}"

    if prepared is None and file_paths:
        prepared = prepare_media_for_upload(file_paths, compress)
    if prepared is not None:
        try:
            if prepared.degrades_to_text:
                # The media can never upload, but the text notice itself is
                # a fresh send: DEFINITELY_NOT_SENT (connect failure / 429)
                # must stay retryable so the notice is not silently lost.
                notice = _send_telegram_text_outcome(api_base, chat_id, text)
                if notice is SendOutcome.DEFINITELY_NOT_SENT:
                    return SendOutcome.DEFINITELY_NOT_SENT
                return SendOutcome.REJECTED_PERMANENT
            if not prepared.files:
                return SendOutcome.REJECTED_PERMANENT
            if prepared.is_video:
                return _send_telegram_video_outcome(api_base, chat_id, text, prepared)
            return _send_telegram_media_group_outcome(api_base, chat_id, text, prepared)
        finally:
            for artifact in prepared.artifacts:
                _cleanup_compressed_artifact(artifact)

    return _send_telegram_text_outcome(api_base, chat_id, text)


def _send_telegram_text_outcome(api_base: str, chat_id: str, text: str) -> SendOutcome:
    """Plain-text send with the same four-way outcome contract."""
    try:
        resp = _telegram_post(
            f"{api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=_TG_TIMEOUT_MESSAGE,
        )
    except requests.RequestException as e:
        if _request_definitely_not_sent(e):
            log.warning(
                "Message never reached Telegram (connect failure): %s",
                _sanitize_exc(e),
            )
            return SendOutcome.DEFINITELY_NOT_SENT
        log.warning(
            "Message send outcome uncertain (post-connect failure): %s",
            _sanitize_exc(e),
        )
        return SendOutcome.UNCERTAIN_NO_ROLLBACK
    if resp.ok:
        return SendOutcome.DELIVERED_OK
    if resp.status_code == 429:
        # Retry-After exceeded the cap or retries exhausted — nothing sent.
        return SendOutcome.DEFINITELY_NOT_SENT
    if 500 <= resp.status_code < 600 or resp.status_code == 408:
        # 5xx: server received (and possibly processed) — uncertain.
        # 408 Request Timeout: the payload may have been received — same.
        return SendOutcome.UNCERTAIN_NO_ROLLBACK
    if resp.status_code in (401, 403):
        # Auth/permission failure: definitively not delivered, retryable
        # once credentials/permissions are fixed — never a silent loss.
        log.warning(
            "Message refused auth-wise (HTTP %d); treated as not sent",
            resp.status_code,
        )
        return SendOutcome.DEFINITELY_NOT_SENT
    # Definitive 4xx: the message was rejected and will never be delivered.
    log.warning("Message permanently rejected (HTTP %d): %s",
                resp.status_code, resp.text[:200])
    return SendOutcome.REJECTED_PERMANENT


def send_to_telegram_detailed(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None = None,
    compress: media_compress.CompressSettings | None = None,
) -> tuple[bool, bool]:
    """Legacy (delivered, media_failed) wrapper over the SendOutcome contract.

    kept for backward compatibility with callers written against the old
    tuple API; new code should use :func:`send_media_to_telegram_outcome`.

    Mapping:
      DELIVERED_OK           -> (True, False)
      DEFINITELY_NOT_SENT    -> (False, True)   retryable — media still owed
      UNCERTAIN_NO_ROLLBACK  -> (False, True)   uncertain — media still owed
      REJECTED_PERMANENT     -> (True, True)    user notified via text notice
    """
    outcome = send_media_to_telegram_outcome(
        bot_token, chat_id, text, file_paths, compress,
    )
    if outcome is SendOutcome.DELIVERED_OK:
        return True, False
    if outcome is SendOutcome.REJECTED_PERMANENT:
        return True, True
    return False, True


def send_to_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None = None,
    compress: media_compress.CompressSettings | None = None,
) -> bool:
    """Send a Telegram message (thin legacy wrapper).

    Returns True when the user-visible push is confirmed, False otherwise.
    """
    ok, _ = send_to_telegram_detailed(bot_token, chat_id, text, file_paths, compress)
    return ok


def _safe_compress(
    compressor, source: Path, target_bytes: int,
    settings: media_compress.CompressSettings,
) -> media_compress.CompressResult:
    """Call a media_compress entry point, turning any crash into a failure value.

    The compressor reports failures as ``CompressResult`` and never raises by
    contract; this belt-and-braces wrapper keeps an unexpected bug (or OOM)
    from escaping the send path, where it would skip the text notice and park
    the item without ever telling the user.
    """
    try:
        return compressor(source, target_bytes=target_bytes, settings=settings)
    except (OSError, ValueError, RuntimeError, MemoryError) as e:
        log.warning(
            "Compression crashed for %s (%s); falling back to the degraded path",
            source.name, e,
        )
        return media_compress.CompressResult(
            ok=False, path=None, original_bytes=source.stat().st_size,
            target_bytes=target_bytes,
            reason=f"compressor crash: {type(e).__name__}: {e}",
        )


def _cleanup_compressed_artifact(path: Path | None) -> None:
    """Best-effort removal of a compressed artifact and its ``.tmp`` sibling.

    The compressed file is a send-time derivative of the original download,
    which stays on disk and is reclaimed later by
    ``monitor.cleanup_old_caches`` (keep_days / max_total_gb).
    """
    if path is None:
        return
    for candidate in (path, path.with_name(path.name + ".tmp")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as e:
            log.debug("Cannot remove compressed artifact %s: %s", candidate, e)


def _degrade_to_text_notice(api_base: str, chat_id: str, text: str) -> SendOutcome:
    """REJECTED_PERMANENT degrade path: at most one plain-text notice.

    Whatever the notice's own outcome is, the outer item stays
    REJECTED_PERMANENT — the media can never be uploaded, so the claim is
    kept and the item never retried. (The notice uses the same outcome
    classifier internally; this path is for items already known to be
    permanently rejected, where a failed notice cannot change that.)
    """
    notice = _send_telegram_text_outcome(api_base, chat_id, text)
    if notice is not SendOutcome.DELIVERED_OK:
        log.warning(
            "Degraded text notice also failed (%s); the item stays "
            "REJECTED_PERMANENT (media can never upload — check manually)",
            notice.value,
        )
    return SendOutcome.REJECTED_PERMANENT


def _classify_media_response(resp: requests.Response, api_base: str,
                             chat_id: str, text: str, kind: str) -> SendOutcome:
    """Map a media-endpoint HTTP response onto the strict outcome enum."""
    if resp.ok:
        return SendOutcome.DELIVERED_OK
    if resp.status_code == 429:
        # Pre-flight refusal surviving the bounded wait: nothing was sent
        # and NO text fallback is allowed (429 is transient pressure, not a
        # rejection — a text notice now would double-notify after the retry).
        log.warning("%s send hit persistent 429; not sent, no text fallback", kind)
        return SendOutcome.DEFINITELY_NOT_SENT
    if 500 <= resp.status_code < 600:
        # The server may have accepted the upload internally — uncertain.
        log.warning(
            "%s send got HTTP %d; outcome uncertain, no text fallback",
            kind, resp.status_code,
        )
        return SendOutcome.UNCERTAIN_NO_ROLLBACK
    if resp.status_code in (401, 403):
        # Auth/permission failure: Telegram definitively never accepted the
        # media, but the item is not inherently unpushable — once the token
        # /permissions are fixed it should retry. NO text fallback (the
        # notice would hit the same 401/403 anyway).
        log.warning(
            "%s send refused auth-wise (HTTP %d); treated as not sent, "
            "claim released for retry", kind, resp.status_code,
        )
        return SendOutcome.DEFINITELY_NOT_SENT
    if resp.status_code == 408:
        # Request Timeout: the payload may have been received — uncertain,
        # so NO text fallback and NO rollback.
        log.warning(
            "%s send got HTTP 408; outcome uncertain, no text fallback", kind,
        )
        return SendOutcome.UNCERTAIN_NO_ROLLBACK
    if 400 <= resp.status_code < 500:
        # Other 4xx: a definitive rejection — the media will never be
        # accepted; the user gets at most one plain-text notice.
        log.warning(
            "%s send rejected (HTTP %d): %s",
            kind, resp.status_code, resp.text[:200],
        )
        return _degrade_to_text_notice(api_base, chat_id, text)
    # Unexpected non-4xx/5xx status (1xx, 3xx, >=600): unknown server
    # semantics — fail to uncertain, never to the text fallback.
    log.warning(
        "%s send got unexpected HTTP %d; outcome uncertain, no text fallback",
        kind, resp.status_code,
    )
    return SendOutcome.UNCERTAIN_NO_ROLLBACK


class PreparedMedia:
    """Upload-ready media plan produced BEFORE the push slot is claimed.

    Running compression and the oversize gate outside the claim critical
    section keeps the .pushed.lock hold time to microseconds — only the
    ledger check + write stays inside.
    """

    __slots__ = ("artifacts", "degrades_to_text", "files", "is_video")

    def __init__(
        self,
        files: list[Path],
        is_video: bool,
        degrades_to_text: bool,
        artifacts: list[Path],
    ) -> None:
        self.files = files
        self.is_video = is_video
        self.degrades_to_text = degrades_to_text
        self.artifacts = artifacts


def prepare_media_for_upload(
    file_paths: list[Path],
    compress: media_compress.CompressSettings | None = None,
) -> PreparedMedia | None:
    """Compress/validate ``file_paths`` into an upload-ready plan.

    Returns ``None`` when no existing file remains (caller should send a
    plain text message). A plan with ``degrades_to_text=True`` means the
    media can never be uploaded (still over the limit after compression):
    the send path issues at most one text notice and resolves
    REJECTED_PERMANENT. Compressed artifacts are cleaned up by the send
    path — do not delete ``plan.artifacts`` here.
    """
    valid_files = [fp for fp in file_paths if fp.exists()]
    if not valid_files:
        return None

    is_video = any(
        fp.suffix.lower() in (".mp4", ".webm", ".mov") for fp in valid_files
    )
    settings = compress or media_compress.CompressSettings()
    artifacts: list[Path] = []

    if is_video:
        video_path = valid_files[0]
        limit_bytes = settings.video_target_bytes
        upload_path = video_path
        size_bytes = video_path.stat().st_size
        if size_bytes > limit_bytes and settings.enabled:
            result = _safe_compress(
                media_compress.compress_video, video_path, limit_bytes, settings,
            )
            log.info("Video compression for %s: %s", video_path.name, result.describe())
            if result.path is not None:
                upload_path = result.path
                artifacts.append(result.path)
                size_bytes = result.output_bytes
        if size_bytes > limit_bytes:
            log.warning(
                "Video %s is %.1f MB > %d MB limit with no usable compressed "
                "artifact (compress.enabled=%s); degrading to text notice",
                video_path.name, size_bytes / 1048576,
                limit_bytes // 1048576, settings.enabled,
            )
            return PreparedMedia([upload_path], True, True, artifacts)
        return PreparedMedia([upload_path], True, False, artifacts)

    # Photo group: at most 10 per sendMediaGroup.
    out_files: list[Path] = []
    for fp in valid_files[:10]:
        try:
            size_bytes = fp.stat().st_size
        except OSError:  # vanished between listing and stat: skip
            continue
        upload_path = fp
        if size_bytes > settings.photo_target_bytes and settings.enabled:
            result = _safe_compress(
                media_compress.compress_image, fp,
                settings.photo_target_bytes, settings,
            )
            log.info("Photo compression for %s: %s", fp.name, result.describe())
            if result.path is not None:
                upload_path = result.path
                artifacts.append(result.path)
                size_bytes = result.output_bytes
        if size_bytes > settings.photo_target_bytes:
            log.warning(
                "Photo %s is %.1f MB > %d MB limit with no usable compressed "
                "artifact (compress.enabled=%s); degrading to text notice",
                fp.name, size_bytes / 1048576,
                settings.photo_target_bytes // 1048576, settings.enabled,
            )
            return PreparedMedia(out_files, False, True, artifacts)
        out_files.append(upload_path)
    return PreparedMedia(out_files, False, False, artifacts)


def _send_telegram_text(api_base: str, chat_id: str, text: str) -> bool:
    """Legacy bool seam for the plain-text send (monkeypatch target).

    Returns True only when the text notice is confirmed delivered.
    """
    return (
        _send_telegram_text_outcome(api_base, chat_id, text)
        is SendOutcome.DELIVERED_OK
    )


def _send_telegram_video_outcome(
    api_base: str, chat_id: str, text: str, prepared: PreparedMedia,
) -> SendOutcome:
    """Upload a prepared video through sendVideo — one attempt, classified."""
    upload_path = prepared.files[0]
    try:
        f = open(upload_path, "rb")  # noqa: SIM115
    except OSError as e:
        # The file vanished / became unreadable BEFORE any byte was sent
        # (e.g. a concurrent cache cleanup): definitively not sent — the
        # claim is released so the next scan can retry the download+push.
        log.warning(
            "Video upload file unreadable before send (%s); "
            "definitely not sent, safe to retry", e,
        )
        return SendOutcome.DEFINITELY_NOT_SENT
    try:
        with f:
            resp = _telegram_post(
                f"{api_base}/sendVideo",
                data={"chat_id": chat_id, "caption": text, "parse_mode": "Markdown"},
                files={"video": (upload_path.name, f, "video/mp4")},
                timeout=_TG_TIMEOUT_VIDEO,
            )
    except requests.RequestException as e:
        if _request_definitely_not_sent(e):
            log.warning(
                "Video send never reached Telegram (connect failure); "
                "safe to retry later: %s", _sanitize_exc(e),
            )
            return SendOutcome.DEFINITELY_NOT_SENT
        log.warning(
            "Video send outcome uncertain (post-connect failure); "
            "claim kept, NO text fallback: %s", _sanitize_exc(e),
        )
        return SendOutcome.UNCERTAIN_NO_ROLLBACK
    return _classify_media_response(resp, api_base, chat_id, text, "Video")


def _send_telegram_media_group_outcome(
    api_base: str, chat_id: str, text: str, prepared: PreparedMedia,
) -> SendOutcome:
    """Upload a prepared photo set via sendMediaGroup — one attempt, classified."""
    media = []
    files: dict[str, tuple] = {}
    open_handles: list = []
    try:
        for i, upload_path in enumerate(prepared.files):
            media.append({
                "type": "photo",
                "media": f"attach://img{i}",
                "caption": text if i == 0 else "",
                "parse_mode": "Markdown",
            })
            try:
                fh = open(upload_path, "rb")  # noqa: SIM115
            except OSError as e:
                # File vanished/became unreadable before any byte was sent:
                # definitively not sent — release the claim for a retry.
                log.warning(
                    "Media group file %s unreadable before send (%s); "
                    "definitely not sent, safe to retry",
                    upload_path.name, e,
                )
                return SendOutcome.DEFINITELY_NOT_SENT
            open_handles.append(fh)
            files[f"img{i}"] = (upload_path.name, fh, "image/jpeg")

        if not media:
            return SendOutcome.REJECTED_PERMANENT

        try:
            resp = _telegram_post(
                f"{api_base}/sendMediaGroup",
                data={"chat_id": chat_id, "media": json.dumps(media)},
                files=files,
                timeout=_TG_TIMEOUT_MEDIA_GROUP,
            )
        except requests.RequestException as e:
            if _request_definitely_not_sent(e):
                log.warning(
                    "Media group send never reached Telegram (connect failure); "
                    "safe to retry later: %s", _sanitize_exc(e),
                )
                return SendOutcome.DEFINITELY_NOT_SENT
            log.warning(
                "Media group outcome uncertain (post-connect failure); "
                "claim kept, NO text fallback: %s", _sanitize_exc(e),
            )
            return SendOutcome.UNCERTAIN_NO_ROLLBACK
        return _classify_media_response(resp, api_base, chat_id, text, "Media group")
    finally:
        for fh in open_handles:
            fh.close()


# ---------------------------------------------------------------------------
# Legacy three-state wrappers (bool | None), kept for monitor re-exports and
# older callers: True = confirmed delivered, False = definitely not sent,
# None = uncertain (must never be retried blindly).
# ---------------------------------------------------------------------------


def _send_telegram_video(
    api_base: str, chat_id: str, text: str, video_path: Path,
    compress: media_compress.CompressSettings | None = None,
) -> bool | None:
    """Legacy thin wrapper: video send outcome only."""
    prepared = prepare_media_for_upload([video_path], compress)
    if prepared is None:
        return False
    try:
        if prepared.degrades_to_text:
            outcome = _degrade_to_text_notice(api_base, chat_id, text)
        else:
            outcome = _send_telegram_video_outcome(api_base, chat_id, text, prepared)
    finally:
        for artifact in prepared.artifacts:
            _cleanup_compressed_artifact(artifact)
    return _outcome_to_legacy_bool(outcome)


def _send_telegram_media_group(
    api_base: str, chat_id: str, text: str, file_paths: list[Path],
    compress: media_compress.CompressSettings | None = None,
) -> bool | None:
    """Legacy thin wrapper: media group outcome only."""
    prepared = prepare_media_for_upload(file_paths, compress)
    if prepared is None:
        return False
    try:
        if prepared.degrades_to_text:
            outcome = _degrade_to_text_notice(api_base, chat_id, text)
        elif not prepared.files:
            return False
        else:
            outcome = _send_telegram_media_group_outcome(api_base, chat_id, text, prepared)
    finally:
        for artifact in prepared.artifacts:
            _cleanup_compressed_artifact(artifact)
    return _outcome_to_legacy_bool(outcome)


def _outcome_to_legacy_bool(outcome: SendOutcome) -> bool | None:
    if outcome is SendOutcome.DELIVERED_OK:
        return True
    if outcome is SendOutcome.UNCERTAIN_NO_ROLLBACK:
        return None
    # REJECTED_PERMANENT attempted its single text notice — terminal either
    # way (True); DEFINITELY_NOT_SENT means safe to retry (False).
    return outcome is SendOutcome.REJECTED_PERMANENT
