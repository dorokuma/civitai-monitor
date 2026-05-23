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
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
import yaml
from filelock import FileLock
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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
    users: list
    mode: str = "incremental"
    nsfw: str = "both"
    api: ApiConfig = Field(default_factory=ApiConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    telegram: TelegramConfig
    data: DataConfig = Field(default_factory=DataConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    video_enabled: bool = True
    max_video_size_mb: int = 500


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


# ---------------------------------------------------------------------------
# Tenacity-retried GET
# ---------------------------------------------------------------------------


@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.RequestException,)),
    reraise=True,
)
def safe_get(url: str, **kwargs) -> requests.Response:
    return session.get(url, timeout=kwargs.pop("timeout", 30), **kwargs)


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
    return mapping[nsfw_setting]


# ---------------------------------------------------------------------------
# Civitai API
# ---------------------------------------------------------------------------


def fetch_page(
    username: str,
    *,
    base_url: str = "https://civitai.com/api/v1",
    limit: int = 100,
    page: int = 1,
    nsfw: bool | None = None,
) -> list[dict[str, Any]]:
    """Fetch one page of images for a user.

    Args:
        nsfw: None → API default | False → SFW | True → NSFW
    """
    params: dict[str, Any] = {
        "username": username,
        "sort": "Newest",
        "limit": limit,
        "page": page,
    }
    if nsfw is not None:
        params["nsfw"] = "true" if nsfw else "false"

    try:
        resp = safe_get(f"{base_url}/images", params=params)
        resp.raise_for_status()
        return resp.json().get("items", [])
    except requests.RequestException as e:
        log.warning("Page %d (nsfw=%s) failed after retries: %s", page, nsfw, e)
        return []


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


def load_seen_ids(path: Path) -> set[int]:
    if path.exists():
        raw = json.loads(path.read_text())
        return set(raw) if isinstance(raw, list) else set()
    return set()


def save_seen_ids(path: Path, ids: set[int]) -> None:
    lock = FileLock(str(LOCK_PATH), timeout=10)
    with lock:
        path.write_text(json.dumps(sorted(ids), indent=2))
    log.info("Saved %d seen IDs to %s", len(ids), path)


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


def download_video(url: str, save_path: Path, max_size_mb: int = 500) -> bool:
    try:
        resp = safe_get(url, stream=True)
        resp.raise_for_status()

        # Check Content-Length header
        content_length = resp.headers.get("content-length")
        if content_length and int(content_length) > max_size_mb * 1024 * 1024:
            log.warning(
                "Video too large (%.1f MB > %d MB), skipping",
                int(content_length) / 1024 / 1024,
                max_size_mb,
            )
            return False

        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        log.info("Video downloaded: %s (%.1f MB)", save_path.name, save_path.stat().st_size / 1024 / 1024)
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
    """Send a video to Telegram. Falls back to text if video exceeds 50 MB limit."""
    MAX_TG_VIDEO = 50 * 1024 * 1024
    size = video_path.stat().st_size
    if size > MAX_TG_VIDEO:
        log.warning("Video %.1f MB exceeds Telegram 50 MB limit — sending text only",
                     size / 1024 / 1024)
        return _send_telegram_text(api_base, chat_id, text)

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
            or "video" in str(item.get("url", "")).lower()
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

        text = (
            f"🎥 *New video by @{username}*\n"
            f"🔗 [View on Civitai]({civitai_url})\n"
            f"🕐 {created_at}"
        )
        send_to_telegram(bot_token, chat_id, text, [filepath] if success else None)
        return success

    # Image path
    orig_url = normalize_to_original(item.get("url", ""), size_suffixes)
    ext = os.path.splitext(orig_url.split("/")[-1])[1] or ".jpeg"
    filepath = output_dir / f"{item_id}{ext}"
    success = download_image(orig_url, filepath)

    text = (
        f"🖼 *New artwork by @{username}*\n"
        f"🔗 [View on Civitai]({civitai_url})\n"
        f"🕐 {created_at}"
    )
    send_to_telegram(bot_token, chat_id, text, [filepath] if success else None)
    return success


# ---------------------------------------------------------------------------
# Incremental mode
# ---------------------------------------------------------------------------


def run_incremental(
    username: str,
    *,
    seen_ids: set[int],
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
    """Check only the latest page(s) — stop as soon as we hit a known ID.

    Returns the set of all image IDs seen on the latest page(s).
    """
    all_seen: set[int] = set()
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = "NSFW" if nsfw_flag else "SFW"
        items = fetch_page(username, base_url=base_url, limit=limit, page=1, nsfw=nsfw_flag)
        if not items:
            continue

        for img in items:
            all_seen.add(img["id"])

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
            log.info("%s: +%d new (track: %s)", username, len(new_on_page), label)
        else:
            log.info("%s: no new (track: %s)", username, label)

    return all_seen


# ---------------------------------------------------------------------------
# Full mode (backfill)
# ---------------------------------------------------------------------------


def run_full(
    username: str,
    *,
    seen_ids: set[int],
    seen_file: Path,
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
    """Walk every page of the user's gallery for the requested tracks.

    Returns the consolidated set of all image IDs seen.
    """
    all_seen: set[int] = set(seen_ids)
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = "NSFW" if nsfw_flag else "SFW"
        log.info("── %s track for @%s ──", label, username)
        page = 1
        consecutive_empty = 0

        while True:
            items = fetch_page(username, base_url=base_url, limit=limit, page=page, nsfw=nsfw_flag)

            if not items:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    log.info("%s: exhausted after page %d", label, page - 1)
                    break
                page += 1
                time.sleep(0.5)
                continue
            consecutive_empty = 0

            new_on_page = [img for img in items if img["id"] not in all_seen]

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
                log.info("%s page %d: +%d new", label, page, len(new_on_page))
            else:
                log.info("%s page %d: all %d already seen", label, page, len(items))

            for img in items:
                all_seen.add(img["id"])

            # Save progress periodically
            if page % 10 == 0:
                save_seen_ids(seen_file, all_seen)

            page += 1
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
    global cfg  # noqa: PLW0602
    cfg = load_config()

    log.info("Mode: %s | NSFW: %s | Video: %s", cfg.mode, cfg.nsfw, cfg.video_enabled)

    # -- Paths --
    data_dir = Path(cfg.data.data_dir) if cfg.data.data_dir else SCRIPT_DIR
    output_dir = data_dir / cfg.download.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    seen_file = data_dir / cfg.data.seen_ids_file

    # -- Parse users --
    users: list[str] = cfg.users
    if users and isinstance(users[0], dict):
        users = [u.get("name", str(u)) if isinstance(u, dict) else str(u) for u in users]
    if not users:
        log.error("No users configured in config.yaml")
        sys.exit(1)

    # -- State --
    seen_ids = load_seen_ids(seen_file)

    # -- Per-user processing --
    consolidated_seen: set[int] = set()

    for username in users:
        log.info("=" * 50)
        log.info("Processing @%s (%s mode)...", username, cfg.mode)

        if cfg.mode == "full":
            user_seen = run_full(
                username,
                seen_ids=seen_ids,
                seen_file=seen_file,
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
        else:
            user_seen = run_incremental(
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
            )

        consolidated_seen.update(user_seen)

    # -- Persist (merge — don't overwrite!) --
    if consolidated_seen:
        union = seen_ids | consolidated_seen
        if len(union) > len(seen_ids):
            save_seen_ids(seen_file, union)
            log.info("Merged %d new IDs into seen_ids (total: %d)",
                     len(union) - len(seen_ids), len(union))

    # -- Cleanup --
    removed = cleanup_old_caches(output_dir, cfg.download.keep_days)
    if removed:
        log.info("Cleaned %d cached files older than %d days", removed, cfg.download.keep_days)

    # -- Full-mode summary --
    if cfg.mode == "full":
        total_new = len(consolidated_seen - seen_ids)
        log.info("=" * 50)
        log.info("Full backfill complete for %s", ", ".join(users))
        log.info("Total new items found: %d", total_new)
        summary = (
            f"✅ *Backfill complete for @{', @'.join(users)}*\n"
            f"📸 Mode: {cfg.mode} · NSFW: {cfg.nsfw} · Video: {cfg.video_enabled}\n"
            f"🆕 New items: {total_new}"
        )
        send_to_telegram(cfg.telegram.bot_token, cfg.telegram.chat_id, summary)


if __name__ == "__main__":
    main()
