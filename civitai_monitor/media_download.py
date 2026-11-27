"""Media URL normalization, path safety guards, and image/video downloaders."""

from __future__ import annotations

import logging
import time as _time_mod
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

import requests

from civitai_client import safe_get as _default_safe_get
from civitai_monitor._compat import mod_attr

log = logging.getLogger("civitai-monitor")

# Per-item timeouts (seconds) for downloads
IMAGE_DOWNLOAD_TIMEOUT = 120
VIDEO_DOWNLOAD_TIMEOUT = 120

# Hard cap for one image download, in MB.
MAX_IMAGE_DOWNLOAD_MB = 30

# Whitelists for the filename extension taken from the last segment of the
# API-provided download URL.
IMAGE_EXT_WHITELIST = frozenset({".jpeg", ".jpg", ".png", ".gif", ".webp"})
VIDEO_EXT_WHITELIST = frozenset({".mp4", ".webm", ".mov", ".mkv", ".avi"})

# HTTP status codes that indicate the resource is gone for good — retrying
# won't help, so the item is flagged as pushed to avoid an infinite loop.
_PERMANENT_HTTP_STATUSES = frozenset({401, 403, 404, 410, 451})


class DownloadResult(NamedTuple):
    """Result of a download attempt.

    ``success`` — file was downloaded and renamed to its final path.
    ``permanent`` — on failure, True means the error won't succeed on retry
    (404/410/403/401/451). Transient failures (timeout, 5xx, connection
    error) set ``permanent=False`` so the caller can keep the item un-pushed
    for the next scan.
    """

    success: bool
    permanent: bool = False


class _VideoSizeCapExceeded(Exception):
    """Internal: stream exceeded max_video_size_mb without Content-Length."""


def _is_permanent_request_error(exc: requests.RequestException) -> bool:
    """Return True for HTTP errors that won't succeed on retry (4xx gone)."""
    resp = getattr(exc, "response", None)
    return resp is not None and resp.status_code in _PERMANENT_HTTP_STATUSES


def _coerce_item_id(raw_id: Any) -> int | None:
    """Coerce an API-supplied item id to ``int``; ``None`` if it is not one."""
    if isinstance(raw_id, bool):
        return None
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def _is_path_within(child: Path, parent: Path) -> bool:
    """True if ``child`` resolves to a location inside ``parent``."""
    try:
        child_resolved = child.resolve()
        parent_resolved = parent.resolve()
    except OSError:
        return False
    return child_resolved == parent_resolved or parent_resolved in child_resolved.parents


def nsfw_tracks(nsfw_setting: str) -> list[bool | None]:
    mapping: dict[str, list[bool | None]] = {
        "sfw_only": [False],
        "nsfw_only": [True],
        "both": [False, True, None],
        "all_only": [None],
    }
    if nsfw_setting not in mapping:
        log.warning("Unknown nsfw setting %r, defaulting to 'both'", nsfw_setting)
    return mapping.get(nsfw_setting, [False, True, None])


def track_label(nsfw_flag: bool | None) -> str:
    if nsfw_flag is True:
        return "NSFW"
    if nsfw_flag is False:
        return "SFW"
    return "ALL"


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


def download_image(
    url: str,
    save_path: Path,
    timeout: int = 120,
    *,
    mod: ModuleType | None = None,
) -> DownloadResult:
    if save_path.exists():
        if save_path.stat().st_size == 0:
            log.warning("Existing %s is 0 bytes; re-downloading", save_path.name)
            save_path.unlink()
        else:
            log.info("Already exists: %s, skipped", save_path.name)
            return DownloadResult(True)
    safe_get_fn = mod_attr(mod, "monitor", "safe_get", _default_safe_get)
    tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")
    max_mb = mod_attr(mod, "monitor", "MAX_IMAGE_DOWNLOAD_MB", MAX_IMAGE_DOWNLOAD_MB)
    max_bytes = max_mb * 1024 * 1024
    try:
        with safe_get_fn(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            save_path.parent.mkdir(parents=True, exist_ok=True)
            downloaded = 0
            exceeded = False
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            log.warning(
                                "Image too large while streaming "
                                "(%.1f MB > %d MB cap), deleting tmp: %s",
                                downloaded / 1024 / 1024,
                                max_mb,
                                url,
                            )
                            exceeded = True
                            break
                        f.write(chunk)
            if exceeded:
                tmp_path.unlink(missing_ok=True)
                return DownloadResult(False, True)
            if downloaded == 0:
                tmp_path.unlink(missing_ok=True)
                log.warning("Image download for %s returned an empty body; will retry", url)
                return DownloadResult(False)
            tmp_path.rename(save_path)
            log.info("Downloaded: %s (%d bytes)", save_path.name, save_path.stat().st_size)
            return DownloadResult(True)
    except requests.RequestException as e:
        tmp_path.unlink(missing_ok=True)
        log.warning("Image download failed for %s: %s", url, e)
        return DownloadResult(False, _is_permanent_request_error(e))


def download_video(
    url: str,
    save_path: Path,
    max_size_mb: int = 1024,
    *,
    mod: ModuleType | None = None,
) -> DownloadResult:
    """Download a full-quality video from Civitai CDN."""
    if save_path.exists():
        if save_path.stat().st_size == 0:
            log.warning("Existing %s is 0 bytes; re-downloading", save_path.name)
            save_path.unlink()
        else:
            log.info("Already exists: %s, skipped", save_path.name)
            return DownloadResult(True)
    safe_get_fn = mod_attr(mod, "monitor", "safe_get", _default_safe_get)
    time_mod = mod_attr(mod, "monitor", "time", _time_mod)
    tmp_path = save_path.with_suffix(save_path.suffix + ".tmp")
    try:
        with safe_get_fn(url, stream=True) as resp:
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
            with safe_get_fn(content_url, stream=True, timeout=120) as resp:
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
                        return DownloadResult(False, True)
                    if not cl and max_bytes:
                        log.info(
                            "Video size unknown (no Content-Length), streaming with %d MB cap",
                            max_size_mb,
                        )
                    save_path.parent.mkdir(parents=True, exist_ok=True)
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
                        if downloaded == 0:
                            tmp_path.unlink(missing_ok=True)
                            log.warning(
                                "Video download returned an empty body (attempt %d/3); will retry",
                                attempt + 1,
                            )
                            last_status = 0
                            continue
                        tmp_path.rename(save_path)
                        log.info(
                            "Video downloaded: %s (%.1f MB)",
                            save_path.name,
                            save_path.stat().st_size / 1024 / 1024,
                        )
                        return DownloadResult(True)
                    except _VideoSizeCapExceeded:
                        try:
                            tmp_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                        return DownloadResult(False, True)
                last_status = resp.status_code
                if resp.status_code == 404:
                    log.warning("B2 /original 404 — video file not available on CDN")
                    return DownloadResult(False, True)
                if attempt < 2:
                    log.info(
                        "B2 /original returned %d, retrying (%d/3)...",
                        resp.status_code, attempt + 2,
                    )
                    time_mod.sleep(2 ** attempt)
        raise requests.HTTPError(f"Video fetch failed (last status {last_status})")
    except requests.RequestException as e:
        tmp_path.unlink(missing_ok=True)
        log.warning("Video download failed for %s: %s", url, e)
        return DownloadResult(False, _is_permanent_request_error(e))
