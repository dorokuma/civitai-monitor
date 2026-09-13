"""Persistence for seen / pushed / pending / inflight ID state."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

log = logging.getLogger("civitai-monitor")

PENDING_CONFIRM_SECONDS = 30 * 60  # 30 minutes
# After one expired retry that is still uncertain, promote without further re-sends
# (caps the "maybe already delivered" loop at a single extra push attempt).
PENDING_MAX_RETRIES = 1

# Pending records: id → (ts, retries, media_failed). ``media_failed`` is 1
# when the media is KNOWN not to have reached Telegram (upload failed after
# the text fallback, or a transient download failure) and 0 when delivery is
# merely uncertain (timeout, crash mid-send). Legacy on-disk values (bare
# float, or {"ts", "retries"} without "media_failed") load with mf=0.
PendingMap = dict[int, tuple[float, int, int]]


class StateWriteError(Exception):
    """Raised when a state file cannot be persisted to disk.

    FileLock timeout after 3 attempts means the caller MUST NOT treat the
    item as saved — otherwise the in-memory state diverges from disk and a
    crash causes a duplicate push. Callers should catch this, skip the
    current item, and let inflight/pending recovery handle it on the next
    scan.
    """


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: tmp → flush+fsync → rename.

    The fsync before rename lowers the window where a system-level crash
    loses the last write (rename is atomic on the same filesystem, but
    the tmp file's data may still be in the page cache).

    OSError (disk full / ENOSPC, permission denied, ...) is wrapped into
    ``StateWriteError`` — same contract as the FileLock ``Timeout`` path —
    so every write entry point (which only catches ``Timeout``) surfaces a
    single error type to callers instead of letting a raw OSError crash
    the monitor process. A half-written tmp file is removed on failure;
    the target file itself is never unlinked (after a successful replace
    the tmp path is already gone, so the cleanup is a no-op then).
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        path.chmod(0o600)
    except OSError as e:
        # Best-effort cleanup; never mask the original write error.
        try:
            tmp.unlink(missing_ok=True)
        except OSError as cleanup_err:
            log.warning("Could not remove tmp state file %s: %s", tmp, cleanup_err)
        raise StateWriteError(
            f"OSError writing state file {path}: {e} (errno: {e.errno})"
        ) from e


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
    """Save seen IDs for a specific (Telegram user, Civitai user) pair.

    Raises ``StateWriteError`` if the file lock cannot be acquired after 3
    attempts — callers must treat the save as failed (the in-memory set
    was not persisted).
    """
    path = seen_file_for_user(seen_dir, tg_id, username)
    lock_path = _save_lock_path(seen_dir, name="seen")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                # Atomic write: temp file + fsync + rename to prevent corruption on crash
                _atomic_write(path, json.dumps(sorted(ids), indent=2))
            log.info("Saved %d seen IDs for @%s", len(ids), username)
            return
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                raise StateWriteError(
                    f"Timeout saving {len(ids)} seen IDs for @{username} "
                    f"after 3 attempts (lock: {lock_path})"
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

    Raises ``StateWriteError`` if the file lock cannot be acquired after 3
    attempts — callers must NOT treat the item as pushed in that case.
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
                _atomic_write(path, json.dumps(sorted(merged), indent=2))
                # Keep caller set in sync with the merged disk view.
                ids.clear()
                ids.update(merged)
            log.info("Saved %d pushed IDs for @%s", len(merged), username)
            return
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                raise StateWriteError(
                    f"Timeout saving {len(ids)} pushed IDs for @{username} "
                    f"after 3 attempts (lock: {lock_path})"
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
    _atomic_write(path, json.dumps(serializable, indent=2))


def update_push_timestamps(
    state_dir: Path,
    kind: str,
    tg_id: str,
    username: str,
    *,
    add: dict[int, float] | None = None,
    remove: set[int] | None = None,
) -> dict[int, float]:
    """Merge add / remove into an inflight-style float map under a file lock.

    Raises ``StateWriteError`` if the lock cannot be acquired after 3 attempts.
    """
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
                raise StateWriteError(
                    f"Timeout updating {kind} push state for @{username} "
                    f"after 3 attempts (lock: {lock_path})"
                )
    return load_push_timestamps(state_dir, kind, tg_id, username)


# ---------------------------------------------------------------------------
# Push history: durable id -> unix-ts log of successful pushes.
#
# Unlike inflight/pending (transient lifecycle state, cleared as soon as a
# push resolves), these entries persist until pruned by age. This is the
# data source for the bot's weekly activity report (per-subscription push
# count over the last 7 days + last push date), which needs to observe
# completed pushes after the fact.
# ---------------------------------------------------------------------------

# Keep ~5 weeks of history: the weekly report window is 7 days and the
# "stale subscription" threshold is 14 days; 35 days bounds file growth
# while leaving ample margin for bot downtime around report time.
PUSH_HISTORY_RETENTION_DAYS = 35


def push_history_file_for_user(pushed_dir: Path, tg_id: str, username: str) -> Path:
    pushed_dir.mkdir(parents=True, exist_ok=True)
    return pushed_dir / f"push_history_{tg_id}_{_safe_user_token(username)}.json"


def load_push_history(pushed_dir: Path, tg_id: str, username: str) -> dict[int, float]:
    """Read-only load of the durable id -> unix-ts push history for one user.

    Corrupt / unreadable / missing file -> empty dict (same policy as
    load_pushed_ids); the weekly report treats empty as "no push on record".
    """
    path = push_history_file_for_user(pushed_dir, tg_id, username)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            return {}
        out: dict[int, float] = {}
        for k, v in raw.items():
            try:
                if isinstance(v, dict):
                    out[int(k)] = float(v.get("ts", 0))
                else:
                    out[int(k)] = float(v)
            except (TypeError, ValueError):
                continue
        return out
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        log.warning("Corrupt push history for @%s, starting empty", username)
        return {}


def record_push_history(
    pushed_dir: Path,
    tg_id: str,
    username: str,
    item_id: int,
    *,
    ts: float | None = None,
) -> None:
    """Append one successful push to the durable history (prunes by age).

    Raises ``StateWriteError`` if the lock cannot be acquired after 3
    attempts. Callers that treat history as best-effort (monitor.py's
    push-success path) wrap this in try/except and never fail a confirmed
    push because of it.
    """
    path = push_history_file_for_user(pushed_dir, tg_id, username)
    lock_path = _save_lock_path(pushed_dir, name="push_history")
    cutoff = time.time() - PUSH_HISTORY_RETENTION_DAYS * 86400
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                data = load_push_history(pushed_dir, tg_id, username)
                data[item_id] = ts if ts is not None else time.time()
                data = {k: v for k, v in data.items() if v >= cutoff}
                _write_push_timestamps(path, data)
            return
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                raise StateWriteError(
                    f"Timeout recording push history for @{username} "
                    f"after 3 attempts (lock: {lock_path})"
                )


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


def _parse_pending_value(v: Any) -> tuple[float, int, int] | None:
    """Parse an on-disk pending value into (ts, retries, media_failed).

    Backward compatible with every historical shape: the current
    {"ts", "retries", "media_failed"} dict, the v1.3.0 {"ts", "retries"} dict
    (media_failed defaults to 0) and the ancient bare-float form.
    """
    try:
        if isinstance(v, dict):
            return (
                float(v.get("ts", 0)),
                int(v.get("retries", 0)),
                1 if v.get("media_failed", 0) else 0,
            )
        return float(v), 0, 0
    except (TypeError, ValueError):
        return None


def _normalize_pending_entry(entry: Any) -> tuple[float, int, int]:
    """Coerce an in-memory pending entry to (ts, retries, media_failed).

    Accepts the extended 3-tuple and the legacy (ts, retries) 2-tuple
    (media_failed defaults to 0) so callers written against the old
    PendingMap shape keep working.
    """
    try:
        ts = float(entry[0])
        retries = int(entry[1])
        mf = entry[2] if len(entry) > 2 else 0
    except (TypeError, ValueError, IndexError) as e:
        raise ValueError(f"malformed pending entry: {entry!r}") from e
    return ts, retries, 1 if mf else 0


def load_pending_map(state_dir: Path, tg_id: str, username: str) -> PendingMap:
    """Load pending id → (ts, retries, media_failed).

    Supports legacy float-only and (ts, retries) values (media_failed 0).
    """
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
        str(k): {"ts": ts, "retries": retries, "media_failed": mf}
        for k, (ts, retries, mf) in sorted(data.items())
    }
    _atomic_write(path, json.dumps(serializable, indent=2))


def update_pending_map(
    state_dir: Path,
    tg_id: str,
    username: str,
    *,
    add: PendingMap | None = None,
    remove: set[int] | None = None,
) -> PendingMap:
    """Merge add/remove into the pending map under lock; atomic rewrite.

    Raises ``StateWriteError`` if the lock cannot be acquired after 3 attempts.
    """
    path = _push_state_file(state_dir, "pending", tg_id, username)
    lock_path = _save_lock_path(state_dir, name="pending_push")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                data = load_pending_map(state_dir, tg_id, username)
                if add:
                    data.update(
                        {iid: _normalize_pending_entry(v) for iid, v in add.items()}
                    )
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
                raise StateWriteError(
                    f"Timeout updating pending push state for @{username} "
                    f"after 3 attempts (lock: {lock_path})"
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
    media_failed: bool = False,
) -> None:
    """Record uncertain delivery (timeout or leftover inflight after crash).

    ``media_failed=True`` marks the record as "the media definitively did
    not leave this machine" (upload failed after the text fallback, or a
    transient download failure). The monitor re-sends such records as soon
    as the item reappears on a page, regardless of PENDING_CONFIRM_SECONDS;
    uncertain (mf=0) records keep the fresh-confirm promotion semantics.
    """
    update_pending_map(
        state_dir, tg_id, username,
        add={
            item_id: (
                ts if ts is not None else time.time(),
                int(retries),
                1 if media_failed else 0,
            )
        },
    )


def clear_pending(state_dir: Path, tg_id: str, username: str, item_id: int) -> None:
    update_pending_map(state_dir, tg_id, username, remove={item_id})


def adopt_stale_inflight(state_dir: Path, tg_id: str, username: str) -> PendingMap:
    """Move any leftover inflight IDs into pending (crash mid-send recovery).

    If a leftover inflight ID already has a pending entry (e.g. the process
    crashed after mark_pending but before clear_inflight in the outcome=None
    path), the existing retries count is preserved so the PENDING_MAX_RETRIES
    cap is not bypassed. The OLDER of the two timestamps wins (a fresh
    inflight stamp must not reset the confirm window mid-retry) and the
    media_failed flag is OR-merged (adoption is delivery-uncertain, so an
    existing mf=1 survives). New inflight IDs (genuine crash mid-send, no
    prior pending) are adopted with retries=0 and media_failed=0 as before.

    Returns the pending map after adoption.
    """
    inflight = load_push_timestamps(state_dir, "inflight", tg_id, username)
    if not inflight:
        return load_pending_map(state_dir, tg_id, username)
    log.warning(
        "Adopting %d leftover inflight ID(s) as pending for @%s (crash recovery)",
        len(inflight), username,
    )
    existing = load_pending_map(state_dir, tg_id, username)
    # Preserve retries for IDs that already have a pending entry; adopt new
    # inflight-only IDs with retries=0 (genuine crash mid-send).
    add: PendingMap = {}
    for iid, ts in inflight.items():
        if iid in existing:
            prev_ts, retries, mf = existing[iid]
            # Keep the OLDER ts: the inflight marker is re-stamped on every
            # send attempt, so adopting its fresh ts would restart the
            # confirm window mid-retry (after a later crash the item would
            # look freshly parked instead of awaiting its retry).
            # mf is OR-merged: adoption is delivery-uncertain (the inflight
            # marker implies nothing about delivery), so it can never clear
            # an existing mf=1 — mf | 0 == mf.
            add[iid] = (min(float(ts), prev_ts), retries, mf)
        else:
            add[iid] = (float(ts), 0, 0)
    update_pending_map(
        state_dir, tg_id, username,
        add=add,
    )
    update_push_timestamps(state_dir, "inflight", tg_id, username, remove=set(inflight.keys()))
    return load_pending_map(state_dir, tg_id, username)


def backlog_alert_file(seen_dir: Path) -> Path:
    """JSON file recording last backlog-truncation Telegram alert per (user, track)."""
    seen_dir.mkdir(parents=True, exist_ok=True)
    return seen_dir / "backlog_truncation_alerts.json"


def claim_backlog_truncation_alert(
    seen_dir: Path,
    username: str,
    track: str,
    today: str | None = None,
) -> bool:
    """Atomically claim today's Telegram slot for ``(username, track)``.

    Returns True if this caller should send the alert (and records *today*
    on disk). Returns False if that pair was already alerted on *today*.
    ``today`` is an ISO date (YYYY-MM-DD); defaults to the local date.

    Cross-process safe via FileLock + ``_atomic_write``. On-disk shape::

        {username: {track: "YYYY-MM-DD"}}
    """
    if not today:
        today = datetime.now(timezone.utc).date().isoformat()
    path = backlog_alert_file(seen_dir)
    lock_path = _save_lock_path(seen_dir, name="backlog_alert")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                state: dict[str, Any] = {}
                if path.exists():
                    try:
                        raw = json.loads(path.read_text())
                        if isinstance(raw, dict):
                            state = raw
                    except (json.JSONDecodeError, OSError, TypeError, ValueError):
                        log.warning(
                            "Corrupt backlog truncation alert file, starting empty"
                        )
                        state = {}
                user_state = state.get(username)
                if not isinstance(user_state, dict):
                    user_state = {}
                if user_state.get(track) == today:
                    return False
                user_state[track] = today
                state[username] = user_state
                _atomic_write(path, json.dumps(state, indent=2))
                return True
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                raise StateWriteError(
                    f"Timeout claiming backlog truncation alert for @{username} "
                    f"track {track} after 3 attempts (lock: {lock_path})"
                )


def rollback_backlog_truncation_alert(
    seen_dir: Path,
    username: str,
    track: str,
    today: str | None = None,
) -> bool:
    """Undo today's claim for ``(username, track)`` so a later scan can retry.

    Returns True if the on-disk slot was cleared. Returns False if there
    was nothing to undo (missing file, missing key, or stored date is not
    *today* — never clobber a different day's record). ``today`` is an ISO
    date (YYYY-MM-DD); defaults to the local date.

    Cross-process safe via FileLock + ``_atomic_write``.
    """
    if not today:
        today = datetime.now(timezone.utc).date().isoformat()
    path = backlog_alert_file(seen_dir)
    lock_path = _save_lock_path(seen_dir, name="backlog_alert")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                if not path.exists():
                    return False
                try:
                    raw = json.loads(path.read_text())
                    if not isinstance(raw, dict):
                        return False
                    state = raw
                except (json.JSONDecodeError, OSError, TypeError, ValueError):
                    log.warning(
                        "Corrupt backlog truncation alert file, skip rollback"
                    )
                    return False
                user_state = state.get(username)
                if not isinstance(user_state, dict):
                    return False
                if user_state.get(track) != today:
                    return False
                del user_state[track]
                if user_state:
                    state[username] = user_state
                else:
                    state.pop(username, None)
                _atomic_write(path, json.dumps(state, indent=2))
                return True
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                raise StateWriteError(
                    f"Timeout rolling back backlog truncation alert for @{username} "
                    f"track {track} after 3 attempts (lock: {lock_path})"
                )
