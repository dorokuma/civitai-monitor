"""Persistence for seen / pushed / pending / inflight ID state."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

log = logging.getLogger("civitai-monitor")

PENDING_CONFIRM_SECONDS = 30 * 60  # 30 minutes
# After one expired retry that is still uncertain, promote without further re-sends
# (caps the "maybe already delivered" loop at a single extra push attempt).
PENDING_MAX_RETRIES = 1

# Pending records: id → (ts, retries). Legacy on-disk value may be a bare float.
PendingMap = dict[int, tuple[float, int]]


def _safe_user_token(username: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "_", username)


def seen_file_for_user(seen_dir: Path, tg_id: str, username: str) -> Path:
    """Get the per-user seen IDs file path.

    Each (Telegram user, Civitai user) pair has its own independent file
    so that different Telegram accounts have separate download progress.
    """
    safe_username = re.sub(r"[^a-zA-Z0-9]", "_", username)
    seen_dir.mkdir(parents=True, exist_ok=True)
    return seen_dir / f"seen_ids_{tg_id}_{safe_username}.json"


def load_seen_ids(seen_dir: Path, tg_id: str, username: str) -> set[int]:
    """Load seen IDs for a specific (Telegram user, Civitai user) pair.

    Corrupt / unreadable files → empty set + warning (same policy as pushed).
    """
    path = seen_file_for_user(seen_dir, tg_id, username)
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        log.warning("Corrupt seen IDs file for @%s, starting empty", username)
        return set()


def _save_lock_path(seen_dir: Path, name: str = "save") -> Path:
    """Per-seen_dir lock file. Keeping the lock inside the data directory
    means the global bot lock doesn't serialize unrelated writes; only
    writers targeting the same seen_dir (which is the only case that can
    actually race) contend."""
    seen_dir.mkdir(parents=True, exist_ok=True)
    return seen_dir / f".{name}.lock"


def save_seen_ids(seen_dir: Path, tg_id: str, username: str, ids: set[int]) -> None:
    """Save seen IDs for a specific (Telegram user, Civitai user) pair."""
    path = seen_file_for_user(seen_dir, tg_id, username)
    lock_path = _save_lock_path(seen_dir, name="seen")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                # Atomic write: temp file + rename to prevent corruption on crash
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(ids), indent=2))
                tmp.rename(path)
                path.chmod(0o600)
            log.info("Saved %d seen IDs for @%s", len(ids), username)
            break
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning(
                    "Timeout saving %d seen IDs for @%s, skipped after 3 attempts",
                    len(ids), username,
                )


def pushed_file_for_user(pushed_dir: Path, tg_id: str, username: str) -> Path:
    safe_username = re.sub(r"[^a-zA-Z0-9]", "_", username)
    pushed_dir.mkdir(parents=True, exist_ok=True)
    return pushed_dir / f"pushed_ids_{tg_id}_{safe_username}.json"


def load_pushed_ids(pushed_dir: Path, tg_id: str, username: str) -> set[int]:
    """Load pushed IDs. Corrupt / unreadable → empty set + warning."""
    path = pushed_file_for_user(pushed_dir, tg_id, username)
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        log.warning("Corrupt pushed IDs file for @%s, starting empty", username)
        return set()


def save_pushed_ids(pushed_dir: Path, tg_id: str, username: str, ids: set[int]) -> None:
    """Persist pushed IDs, merging with any on-disk set under the lock.

    Merge-on-write avoids clobbering IDs saved by an earlier crash-recovery
    path or a concurrent writer. The caller's ``ids`` set is updated in place
    to the merged result so in-memory state stays consistent with disk.
    """
    path = pushed_file_for_user(pushed_dir, tg_id, username)
    lock_path = _save_lock_path(pushed_dir, name="pushed")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                on_disk: set[int] = set()
                if path.exists():
                    try:
                        on_disk = set(json.loads(path.read_text()))
                    except (json.JSONDecodeError, OSError, TypeError, ValueError):
                        log.warning(
                            "Corrupt pushed IDs file for @%s, rewriting from memory",
                            username,
                        )
                merged = on_disk | set(ids)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(sorted(merged), indent=2))
                tmp.rename(path)
                path.chmod(0o600)
                # Keep caller set in sync with the merged disk view.
                ids.clear()
                ids.update(merged)
            log.info("Saved %d pushed IDs for @%s", len(merged), username)
            break
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning(
                    "Timeout saving %d pushed IDs for @%s, skipped after 3 attempts",
                    len(ids), username,
                )


# ---------------------------------------------------------------------------
# Push lifecycle state: inflight (pre-claim) + pending (timeout / uncertain)
# ---------------------------------------------------------------------------


def _push_state_file(state_dir: Path, kind: str, tg_id: str, username: str) -> Path:
    """kind is 'pending' or 'inflight'."""
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"{kind}_push_{tg_id}_{_safe_user_token(username)}.json"


def load_push_timestamps(state_dir: Path, kind: str, tg_id: str, username: str) -> dict[int, float]:
    """Load id → unix-ts map for inflight (simple floats). Corrupt/missing → empty."""
    path = _push_state_file(state_dir, kind, tg_id, username)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {}
        out: dict[int, float] = {}
        for k, v in raw.items():
            try:
                # Allow legacy pending-style dicts if misread as inflight.
                if isinstance(v, dict):
                    out[int(k)] = float(v.get("ts", 0))
                else:
                    out[int(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        log.warning("Corrupt %s push state for @%s, starting empty", kind, username)
        return {}


def _write_push_timestamps(path: Path, data: dict[int, float]) -> None:
    """Atomic rewrite of an id→ts map (caller must hold the lock)."""
    serializable = {str(k): v for k, v in sorted(data.items())}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serializable, indent=2))
    tmp.rename(path)
    path.chmod(0o600)


def update_push_timestamps(
    state_dir: Path,
    kind: str,
    tg_id: str,
    username: str,
    *,
    add: dict[int, float] | None = None,
    remove: set[int] | None = None,
) -> dict[int, float]:
    """Merge add / remove into an inflight-style float map under a file lock."""
    path = _push_state_file(state_dir, kind, tg_id, username)
    lock_path = _save_lock_path(state_dir, name=f"{kind}_push")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                data = load_push_timestamps(state_dir, kind, tg_id, username)
                if add:
                    data.update(add)
                if remove:
                    for iid in remove:
                        data.pop(iid, None)
                if data:
                    _write_push_timestamps(path, data)
                elif path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        _write_push_timestamps(path, {})
                return data
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning(
                    "Timeout updating %s push state for @%s after 3 attempts",
                    kind, username,
                )
    return load_push_timestamps(state_dir, kind, tg_id, username)


def mark_inflight(state_dir: Path, tg_id: str, username: str, item_id: int) -> None:
    """Pre-claim an item ID before the Telegram request leaves the process."""
    update_push_timestamps(
        state_dir, "inflight", tg_id, username,
        add={item_id: time.time()},
    )


def clear_inflight(state_dir: Path, tg_id: str, username: str, item_id: int) -> None:
    update_push_timestamps(
        state_dir, "inflight", tg_id, username,
        remove={item_id},
    )


def _parse_pending_value(v: Any) -> tuple[float, int] | None:
    try:
        if isinstance(v, dict):
            return float(v.get("ts", 0)), int(v.get("retries", 0))
        return float(v), 0
    except (TypeError, ValueError):
        return None


def load_pending_map(state_dir: Path, tg_id: str, username: str) -> PendingMap:
    """Load pending id → (ts, retries). Supports legacy float-only values."""
    path = _push_state_file(state_dir, "pending", tg_id, username)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {}
        out: PendingMap = {}
        for k, v in raw.items():
            parsed = _parse_pending_value(v)
            if parsed is None:
                continue
            try:
                out[int(k)] = parsed
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        log.warning("Corrupt pending push state for @%s, starting empty", username)
        return {}


def _write_pending_map(path: Path, data: PendingMap) -> None:
    serializable = {
        str(k): {"ts": ts, "retries": retries}
        for k, (ts, retries) in sorted(data.items())
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(serializable, indent=2))
    tmp.rename(path)
    path.chmod(0o600)


def update_pending_map(
    state_dir: Path,
    tg_id: str,
    username: str,
    *,
    add: PendingMap | None = None,
    remove: set[int] | None = None,
) -> PendingMap:
    """Merge add/remove into the pending map under lock; atomic rewrite."""
    path = _push_state_file(state_dir, "pending", tg_id, username)
    lock_path = _save_lock_path(state_dir, name="pending_push")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                data = load_pending_map(state_dir, tg_id, username)
                if add:
                    data.update(add)
                if remove:
                    for iid in remove:
                        data.pop(iid, None)
                if data:
                    _write_pending_map(path, data)
                elif path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        _write_pending_map(path, {})
                return data
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                log.warning(
                    "Timeout updating pending push state for @%s after 3 attempts",
                    username,
                )
    return load_pending_map(state_dir, tg_id, username)


def mark_pending(
    state_dir: Path,
    tg_id: str,
    username: str,
    item_id: int,
    *,
    ts: float | None = None,
    retries: int = 0,
) -> None:
    """Record uncertain delivery (timeout or leftover inflight after crash)."""
    update_pending_map(
        state_dir, tg_id, username,
        add={item_id: (ts if ts is not None else time.time(), int(retries))},
    )


def clear_pending(state_dir: Path, tg_id: str, username: str, item_id: int) -> None:
    update_pending_map(state_dir, tg_id, username, remove={item_id})


def adopt_stale_inflight(state_dir: Path, tg_id: str, username: str) -> PendingMap:
    """Move any leftover inflight IDs into pending (crash mid-send recovery).

    Returns the pending map after adoption.
    """
    inflight = load_push_timestamps(state_dir, "inflight", tg_id, username)
    if not inflight:
        return load_pending_map(state_dir, tg_id, username)
    log.warning(
        "Adopting %d leftover inflight ID(s) as pending for @%s (crash recovery)",
        len(inflight), username,
    )
    update_pending_map(
        state_dir, tg_id, username,
        add={iid: (ts, 0) for iid, ts in inflight.items()},
    )
    update_push_timestamps(state_dir, "inflight", tg_id, username, remove=set(inflight.keys()))
    return load_pending_map(state_dir, tg_id, username)
