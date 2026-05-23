#!/usr/bin/env python3
"""
Civitai User Gallery Monitor

Monitors specified Civitai users for new image uploads, downloads
full-resolution originals, and pushes them to a Telegram channel
via the Bot API.

Supports two modes (configurable in config.yaml):
  - incremental (default): check only the latest images — ideal for cron
  - full: walk every page of the user's gallery — for initial backfill

Supports NSFW filtering:
  - sfw_only: only safe-for-work images
  - nsfw_only: only NSFW images
  - both: pull SFW and NSFW (recommended)

Usage:
  python3 monitor.py                          # uses config.yaml
  python3 monitor.py --config /path/to.yaml   # custom config path
"""

from __future__ import annotations

import json
import os
import sys
import time
import logging
from pathlib import Path
from typing import Any

import requests
import yaml

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("civitai-monitor")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path.home() / ".civitai-monitor" / "config.yaml",
    Path(__file__).parent / "config.yaml",
]

SCRIPT_DIR = Path(__file__).parent.resolve()

VALID_NSFW = {"sfw_only", "nsfw_only", "both"}
VALID_MODES = {"incremental", "full"}

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> dict[str, Any]:
    paths = [Path(path)] if path else DEFAULT_CONFIG_PATHS
    for p in paths:
        if p.exists():
            log.info("Loading config from %s", p)
            with open(p, encoding="utf-8") as f:
                return dict(yaml.safe_load(f))
    log.error("config.yaml not found (searched: %s)", [str(p) for p in paths])
    print(json.dumps({"error": "config.yaml not found", "searched": [str(p) for p in paths]}))
    sys.exit(1)


def resolve_mode(cfg: dict[str, Any]) -> str:
    mode = str(cfg.get("mode", "incremental")).lower()
    if mode not in VALID_MODES:
        log.warning("Unknown mode '%s', falling back to 'incremental'", mode)
        return "incremental"
    return mode


def resolve_nsfw(cfg: dict[str, Any]) -> str:
    nsfw = str(cfg.get("nsfw", "both")).lower()
    if nsfw not in VALID_NSFW:
        log.warning("Unknown nsfw '%s', falling back to 'both'", nsfw)
        return "both"
    return nsfw


def nsfw_tracks(nsfw_setting: str) -> list[bool | None]:
    """Return the list of API nsfw parameter values to query.

    Returns:
      [False]        for sfw_only
      [True]         for nsfw_only
      [False, True]  for both

    None = don't pass nsfw param at all (API default = SFW only).
    We use explicit False/True for clarity.
    """
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
    retries: int = 3,
) -> list[dict[str, Any]]:
    """Fetch one page of images for a user.

    Args:
        nsfw: None → API default (SFW only, per current API behaviour)
              False → only SFW
              True  → only NSFW
    """
    params: dict[str, Any] = {
        "username": username,
        "sort": "Newest",
        "limit": limit,
        "page": page,
    }
    if nsfw is not None:
        params["nsfw"] = "true" if nsfw else "false"

    headers = {"User-Agent": "CivitaiMonitor/2.0"}

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                f"{base_url}/images", params=params, headers=headers, timeout=15
            )
            resp.raise_for_status()
            return resp.json().get("items", [])
        except requests.RequestException as e:
            if attempt < retries:
                log.warning(
                    "Page %d (nsfw=%s) attempt %d/%d failed: %s",
                    page, nsfw, attempt, retries, e,
                )
                time.sleep(2**attempt)
            else:
                log.error(
                    "Page %d (nsfw=%s) failed after %d attempts: %s",
                    page, nsfw, retries, e,
                )
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
# Downloaded-image tracking
# ---------------------------------------------------------------------------


def load_seen_ids(path: Path) -> set[int]:
    if path.exists():
        raw = json.loads(path.read_text())
        return set(raw) if isinstance(raw, list) else set()
    return set()


def save_seen_ids(path: Path, ids: set[int]) -> None:
    path.write_text(json.dumps(sorted(ids), indent=2))
    log.info("Saved %d seen IDs to %s", len(ids), path)


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------


def download_image(url: str, save_path: Path, timeout: int = 60) -> bool:
    headers = {
        "User-Agent": "CivitaiMonitor/2.0",
        "Referer": "https://civitai.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(resp.content)
        log.info("Downloaded: %s (%d bytes)", save_path.name, len(resp.content))
        return True
    except requests.RequestException as e:
        log.warning("Download failed for %s: %s", url, e)
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
        media = []
        for i, fp in enumerate(file_paths):
            if fp.exists():
                media.append({
                    "type": "photo",
                    "media": f"attach://img{i}",
                    "caption": text if i == 0 else "",
                    "parse_mode": "Markdown",
                })
        if media:
            files = {
                f"img{i}": (fp.name, fp.read_bytes(), "image/jpeg")
                for i, fp in enumerate(file_paths)
                if fp.exists()
            }
            try:
                resp = requests.post(
                    f"{api_base}/sendMediaGroup",
                    data={"chat_id": chat_id, "media": json.dumps(media)},
                    files=files,
                    timeout=30,
                )
                if resp.ok:
                    return True
                log.warning("Media group send failed: %s", resp.text[:200])
            except requests.RequestException as e:
                log.warning("Media group error: %s", e)

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
# Download-and-push helper (shared by incremental & full modes)
# ---------------------------------------------------------------------------


def process_and_push(
    img: dict[str, Any],
    username: str,
    *,
    size_suffixes: list[str],
    output_dir: Path,
    bot_token: str,
    chat_id: str,
) -> bool:
    """Download a single image and push to Telegram. Returns True on success."""
    img_id = img["id"]
    orig_url = normalize_to_original(img.get("url", ""), size_suffixes)
    civitai_url = f"https://civitai.com/images/{img_id}"
    created_at = img.get("createdAt", "")

    ext = os.path.splitext(orig_url.split("/")[-1])[1] or ".jpeg"
    filename = f"{img_id}{ext}"
    filepath = output_dir / filename

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
) -> set[int]:
    """Check only the latest page(s) — stop as soon as we hit a known ID.

    Returns the set of all image IDs seen in this run (new + already-known).
    """
    all_seen: set[int] = set()
    tracks = nsfw_tracks(nsfw_setting)

    for nsfw_flag in tracks:
        label = "NSFW" if nsfw_flag else "SFW"
        items = fetch_page(username, base_url=base_url, limit=limit, page=1, nsfw=nsfw_flag)
        if not items:
            continue

        for img in items:
            img_id = img["id"]
            all_seen.add(img_id)

        # Process only images not yet seen
        new_on_page = [img for img in items if img["id"] not in seen_ids]
        if new_on_page:
            # Push in chronological order (oldest first)
            for img in reversed(new_on_page):
                process_and_push(
                    img, username,
                    size_suffixes=size_suffixes, output_dir=output_dir,
                    bot_token=bot_token, chat_id=chat_id,
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
    nsfw_setting: str,
    output_dir: Path,
    size_suffixes: list[str],
    bot_token: str,
    chat_id: str,
    base_url: str,
    limit: int,
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

            # Filter out already-seen
            new_on_page = [img for img in items if img["id"] not in all_seen]

            if new_on_page:
                for img in reversed(new_on_page):
                    process_and_push(
                        img, username,
                        size_suffixes=size_suffixes, output_dir=output_dir,
                        bot_token=bot_token, chat_id=chat_id,
                    )
                    time.sleep(0.5)
                log.info("%s page %d: +%d new", label, page, len(new_on_page))
            else:
                log.info("%s page %d: all %d already seen", label, page, len(items))

            # Record all IDs on this page
            for img in items:
                all_seen.add(img["id"])

            # Save progress periodically (every 10 pages)
            if page % 10 == 0:
                save_seen_ids(seen_file := Path(cfg.get("data", {}).get("seen_ids_file", "seen_ids.json")), all_seen)  # noqa: PLW2901

            page += 1
            time.sleep(0.5)

    return all_seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    global cfg  # noqa: PLW0602 — used by full mode progress-saving
    cfg = load_config()

    # -- Resolve operational mode --
    mode = resolve_mode(cfg)
    nsfw_setting = resolve_nsfw(cfg)
    log.info("Mode: %s | NSFW: %s", mode, nsfw_setting)

    # -- Paths --
    data_dir = Path(cfg.get("data", {}).get("data_dir", SCRIPT_DIR))
    output_dir = data_dir / cfg.get("download", {}).get("output_dir", "downloads")
    seen_file = data_dir / cfg.get("data", {}).get("seen_ids_file", "seen_ids.json")
    output_dir.mkdir(parents=True, exist_ok=True)

    # -- Parse users --
    users: list[str] = cfg.get("users", [])
    if isinstance(users, list) and users and isinstance(users[0], dict):
        users = [u.get("name", str(u)) if isinstance(u, dict) else str(u) for u in users]
    if not users:
        log.error("No users configured in config.yaml")
        sys.exit(1)

    # -- Download settings --
    size_suffixes = cfg.get("download", {}).get("size_suffixes", [
        "/width=1024/", "/width=450/", "/width=640/",
    ])

    # -- API settings --
    api_cfg = cfg.get("api", {})
    base_url = api_cfg.get("base_url", "https://civitai.com/api/v1")
    limit = api_cfg.get("images_per_page", 100)

    # -- Telegram credentials --
    telegram_cfg = cfg.get("telegram", {})
    bot_token: str = telegram_cfg.get("bot_token", "") or ""
    chat_id: str = telegram_cfg.get("chat_id", "") or ""
    if not bot_token or not chat_id:
        log.error("telegram.bot_token and telegram.chat_id are required in config.yaml")
        sys.exit(1)

    # -- State --
    seen_ids = load_seen_ids(seen_file)

    # -- Per-user processing --
    consolidated_seen: set[int] = set()

    for username in users:
        log.info("=" * 50)
        log.info("Processing @%s (%s mode)...", username, mode)

        if mode == "full":
            user_seen = run_full(
                username,
                seen_ids=seen_ids,
                nsfw_setting=nsfw_setting,
                output_dir=output_dir,
                size_suffixes=size_suffixes,
                bot_token=bot_token,
                chat_id=chat_id,
                base_url=base_url,
                limit=limit,
            )
        else:
            user_seen = run_incremental(
                username,
                seen_ids=seen_ids,
                nsfw_setting=nsfw_setting,
                output_dir=output_dir,
                size_suffixes=size_suffixes,
                bot_token=bot_token,
                chat_id=chat_id,
                base_url=base_url,
                limit=limit,
            )

        consolidated_seen.update(user_seen)

    # -- Persist --
    if consolidated_seen:
        save_seen_ids(seen_file, consolidated_seen)

    if mode == "full":
        total_new = len(consolidated_seen - seen_ids)
        log.info("=" * 50)
        log.info("Full backfill complete for %s", ", ".join(users))
        log.info("Total new images found: %d", total_new)
        summary = (
            f"✅ *Backfill complete for @{', @'.join(users)}*\n"
            f"📸 Mode: {mode} · NSFW: {nsfw_setting}\n"
            f"🆕 New images found: {total_new}"
        )
        send_to_telegram(bot_token, chat_id, summary)


if __name__ == "__main__":
    main()
