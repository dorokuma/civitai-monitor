#!/usr/bin/env python3
"""
Civitai User Gallery Monitor

Monitors specified Civitai users for new image uploads.
Supports two notification modes:
  - Hermes mode (default): outputs JSON to stdout for Hermes cronjob agent
  - Direct mode: sends to Telegram directly if bot credentials configured

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

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load configuration from YAML file.

    Searches DEFAULT_CONFIG_PATHS if no path given.  All keys that reference
    external credentials MUST be placeholders in the example file.
    """
    paths = [Path(path)] if path else DEFAULT_CONFIG_PATHS

    for p in paths:
        if p.exists():
            log.info("Loading config from %s", p)
            with open(p, encoding="utf-8") as f:
                cfg: dict[str, Any] = yaml.safe_load(f)
            return cfg

    print(
        json.dumps(
            {
                "error": "config.yaml not found",
                "searched": [str(p) for p in paths],
                "hint": "Copy config.yaml.example to config.yaml and fill in your settings.",
            }
        )
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Civitai API
# ---------------------------------------------------------------------------


def fetch_user_images(
    username: str,
    *,
    base_url: str = "https://civitai.com/api/v1",
    limit: int = 20,
    retries: int = 3,
) -> list[dict[str, Any]]:
    """Fetch the latest images for a given Civitai user.

    Retries up to ``retries`` times on transient failures.
    """
    url = f"{base_url}/images"
    params = {"username": username, "sort": "Newest", "limit": limit}
    headers = {"User-Agent": "CivitaiMonitor/2.0"}

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items", [])
            log.info("Fetched %d images for @%s", len(items), username)
            return items
        except requests.RequestException as e:
            last_error = e
            log.warning("Attempt %d/%d failed for @%s: %s", attempt, retries, username, e)
            if attempt < retries:
                time.sleep(2**attempt)  # exponential back-off

    log.error("All %d attempts failed for @%s: %s", retries, username, last_error)
    return []


# ---------------------------------------------------------------------------
# Image URL processing
# ---------------------------------------------------------------------------


def normalize_to_original(
    image_url: str,
    size_suffixes: list[str] | None = None,
) -> str:
    """Replace size-limited CDN URLs with ``width=original``.

    Example:
      ``.../width=1024/xxx.jpeg`` → ``.../width=original/xxx.jpeg``
    """
    if size_suffixes is None:
        size_suffixes = ["/width=1024/", "/width=450/", "/width=640/"]

    result = image_url
    for suffix in size_suffixes:
        if suffix in result:
            result = result.replace(suffix, "/width=original/")
            break
    return result


# ---------------------------------------------------------------------------
# Downloaded-image tracking
# ---------------------------------------------------------------------------


def load_seen_ids(path: Path) -> set[int]:
    """Load the set of already-processed image IDs."""
    if path.exists():
        raw = json.loads(path.read_text())
        return set(raw) if isinstance(raw, list) else set()
    return set()


def save_seen_ids(path: Path, ids: set[int]) -> None:
    """Persist the set of processed image IDs."""
    path.write_text(json.dumps(sorted(ids), indent=2))
    log.info("Saved %d seen IDs to %s", len(ids), path)


# ---------------------------------------------------------------------------
# Image download
# ---------------------------------------------------------------------------


def download_image(url: str, save_path: Path, timeout: int = 60) -> bool:
    """Download an image to disk. Returns True on success."""
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
# Telegram direct push (Direct mode only)
# ---------------------------------------------------------------------------


def send_to_telegram(
    bot_token: str,
    chat_id: str,
    text: str,
    file_paths: list[Path] | None = None,
) -> bool:
    """Push a message (with optional media group) to a Telegram chat.

    Uses Telegram Bot API directly.  Falls back to text-only if media
    upload fails.
    """
    api_base = f"https://api.telegram.org/bot{bot_token}"

    if file_paths:
        # Try sending as media group (up to 10 images)
        media = []
        for i, fp in enumerate(file_paths):
            if fp.exists():
                media.append(
                    {
                        "type": "photo",
                        "media": f"attach://img{i}",
                        "caption": text if i == 0 else "",
                        "parse_mode": "Markdown",
                    }
                )

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
                log.warning("Media group send failed: %s", resp.text)
            except requests.RequestException as e:
                log.warning("Media group send error: %s", e)

    # Fallback: text-only message
    try:
        resp = requests.post(
            f"{api_base}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=15,
        )
        return resp.ok
    except requests.RequestException as e:
        log.error("Telegram message send failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Per-user processing
# ---------------------------------------------------------------------------


def process_user(
    username: str,
    cfg: dict[str, Any],
    *,
    seen_ids: set[int],
    output_dir: Path,
    size_suffixes: list[str],
) -> tuple[list[dict[str, Any]], set[int]]:
    """Check one user for new images.

    Returns (results, all_seen_ids_for_this_user).
    """
    api_cfg = cfg.get("api", {})
    base_url = api_cfg.get("base_url", "https://civitai.com/api/v1")
    limit = api_cfg.get("images_per_page", 20)

    images = fetch_user_images(username, base_url=base_url, limit=limit)

    new_results: list[dict[str, Any]] = []
    user_seen: set[int] = set()

    for img in images:
        img_id = img["id"]
        user_seen.add(img_id)

        if img_id in seen_ids:
            # Because results are sorted Newest-first, the first hit means
            # all remaining items are already processed.
            break

        orig_url = normalize_to_original(img.get("url", ""), size_suffixes)
        civitai_url = f"https://civitai.com/images/{img_id}"
        created_at = img.get("createdAt", "")

        # Determine file extension from URL
        ext = os.path.splitext(orig_url.split("/")[-1])[1] or ".jpeg"
        filename = f"{img_id}{ext}"
        filepath = output_dir / filename

        success = download_image(orig_url, filepath)

        entry = {
            "id": img_id,
            "username": username,
            "civitai_url": civitai_url,
            "created_at": created_at,
            "nsfw": img.get("nsfw", False),
        }

        if success:
            entry["image_path"] = str(filepath)
        else:
            entry["image_url"] = orig_url
            entry["download_error"] = "download_failed"

        new_results.append(entry)

    # If no images were returned (API failure), don't wipe seen_ids
    if images:
        # Also add all fetched IDs to seen
        for img in images:
            user_seen.add(img["id"])

    return new_results, user_seen


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    cfg = load_config()

    # -- Paths --
    data_dir = Path(cfg.get("data", {}).get("data_dir", SCRIPT_DIR))
    output_dir = data_dir / cfg.get("download", {}).get("output_dir", "downloads")
    seen_file = data_dir / cfg.get("data", {}).get("seen_ids_file", "seen_ids.json")
    output_dir.mkdir(parents=True, exist_ok=True)

    # -- Config --
    users: list[str] = cfg.get("users", [])
    if isinstance(users, list) and len(users) > 0 and isinstance(users[0], dict):
        # Support both list-of-strings and list-of-dicts
        users = [u.get("name", str(u)) if isinstance(u, dict) else str(u) for u in users]

    if not users:
        log.error("No users configured in config.yaml")
        print(json.dumps({"error": "No users configured", "users": users}))
        sys.exit(1)

    size_suffixes = cfg.get("download", {}).get("size_suffixes", [
        "/width=1024/",
        "/width=450/",
        "/width=640/",
    ])

    # -- State --
    seen_ids = load_seen_ids(seen_file)

    # -- Process each user --
    all_results: list[dict[str, Any]] = []
    all_new_ids: set[int] = set()

    for username in users:
        results, user_seen = process_user(
            username,
            cfg,
            seen_ids=seen_ids,
            output_dir=output_dir,
            size_suffixes=size_suffixes,
        )
        all_results.extend(results)
        all_new_ids.update(user_seen)

    # -- Persist seen IDs --
    if all_new_ids:
        save_seen_ids(seen_file, all_new_ids)

    # -- Output / Notify --
    notifier_cfg = cfg.get("notifier", {})
    mode = notifier_cfg.get("mode", "hermes")

    if mode == "direct":
        # Direct Telegram push mode
        bot_token = notifier_cfg.get("telegram_bot_token", "")
        chat_id = notifier_cfg.get("telegram_chat_id", "")

        if not bot_token or not chat_id:
            log.error("Direct mode requires telegram_bot_token and telegram_chat_id")
            print(json.dumps({"error": "Direct mode missing credentials"}))
            sys.exit(1)

        if not all_results:
            log.info("No new images — nothing to push")
            return

        # Group results by user for cleaner messages
        for result in all_results:
            lines = [
                f"🖼 *New artwork by @{result['username']}*",
                f"🔗 [View on Civitai]({result['civitai_url']})",
                f"🕐 {result.get('created_at', 'unknown')}",
            ]
            text = "\n".join(lines)

            file_paths = []
            if "image_path" in result:
                file_paths.append(Path(result["image_path"]))

            send_to_telegram(bot_token, chat_id, text, file_paths)

        log.info("Pushed %d new images to Telegram", len(all_results))

    else:
        # Hermes mode (default) — output JSON to stdout
        if not all_results:
            return  # silent exit — nothing new

        print(json.dumps(all_results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
