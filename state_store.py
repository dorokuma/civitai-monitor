"""Persistence for seen / pushed / claimed ID state (strict at-most-once).

State files live in the per-install data directory:

  seen_ids_{tg_id}_{user}.json        — downloaded progress per subscriber/creator
  pushed_ids_{tg_id}_{user}.json      — per (subscriber, creator) pushed ledger
                                        (kept for hole-window progress and the
                                        weekly report; maintained in sync with
                                        the global chat ledger below)
  pushed_chat_{safe_chat_id}.json     — GLOBAL dedup ledger keyed by the target
                                        chat: an ID in here must NEVER be sent
                                        to that chat again, regardless of which
                                        (tg_id, username) pair is processing it.
  .pushed.lock                        — shared FileLock for every pushed-ledger
                                        atomic primitive.

Fail-closed read contract: if a ledger file exists but cannot be read or
parsed (OSError, JSONDecodeError, non-integer payload), a StateReadError is
raised. NEVER fall back to an empty set — treating a corrupted ledger as
empty is exactly how duplicate pushes happen.
"""

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

# Kept for backward compatibility with monitor.py / existing imports. The
# pending auto-resend machinery was removed by the strict at-most-once
# rework: PENDING_MAX_RETRIES=0 means "a pending record is terminal".
PENDING_CONFIRM_SECONDS = 30 * 60  # 30 minutes
PENDING_MAX_RETRIES = 0

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
    crash causes a duplicate push.
    """


class StateReadError(StateWriteError):
    """Raised when an EXISTING state file cannot be read or parsed.

    Fail-closed: a corrupted or unreadable pushed ledger must abort the send
    path (0 POSTs) rather than silently assume "not pushed" — the latter is
    exactly how duplicate pushes happen. Missing files are fine (empty set);
    only a present-but-broken file triggers this. Subclasses StateWriteError
    so existing ``except StateWriteError`` handlers treat it as a fatal
    state error too.
    """


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: tmp → flush+fsync → rename.

    The fsync before rename lowers the window where a system-level crash
    loses the last write (rename is atomic on the same filesystem, but
    the tmp file's data may still be in the page cache). After the replace
    the PARENT DIRECTORY is fsynced so the rename itself is durable; on
    filesystems that do not support directory fsync the EINVAL/EBADF is
    ignored (the file-level fsync + atomic rename still apply).

    OSError (disk full / ENOSPC, permission denied, ...) is wrapped into
    ``StateWriteError`` so every write entry point surfaces a single error
    type to callers. A half-written tmp file is removed on failure; the
    target file itself is never unlinked.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError as e:
                # Filesystems without directory fsync (EINVAL/EBADF) — the
                # rename still happened; durability is best-effort there.
                if e.errno not in (22, 9):  # EINVAL / EBADF
                    raise
            finally:
                os.close(dir_fd)
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


def _safe_chat_token(chat_id: str) -> str:
    """Filesystem-safe token for a chat id (``-100123`` → ``_100123``)."""
    return re.sub(r"[^a-zA-Z0-9]", "_", str(chat_id))


def _read_int_set_strict(path: Path) -> set[int]:
    """Read a JSON int-array ledger FAIL-CLOSED.

    Missing file → empty set. An existing file that cannot be read
    (OSError), parsed (JSONDecodeError) or coerced to ``set[int]``
    (non-array top level, non-integer members) raises ``StateReadError``;
    the corrupted original is left untouched on disk.
    """
    if not path.exists():
        return set()
    try:
        raw = json.loads(path.read_text())
        if not isinstance(raw, list):
            raise TypeError(f"ledger top level is {type(raw).__name__}, not a list")
        return {int(v) for v in raw}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        raise StateReadError(
            f"Unreadable state ledger {path}: {type(e).__name__}: {e} "
            "(fail-closed: NOT falling back to an empty set)"
        ) from e


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

    Corrupt / unreadable files → empty set + warning (the seen ledger only
    tracks download progress — a lost entry costs a re-download, never a
    duplicate push, so the fail-open policy stays for seen only).
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
    """Per (tg_id, creator) pushed-ledger path.

    Kept alongside the global chat ledger: it feeds hole-window progress
    (run_incremental's safe_lower_bound) and the weekly report. Every write
    through the claim/rollback primitives keeps the two ledgers in sync.
    """
    safe_username = re.sub(r"[^a-zA-Z0-9]", "_", username)
    pushed_dir.mkdir(parents=True, exist_ok=True)
    return pushed_dir / f"pushed_ids_{tg_id}_{safe_username}.json"


def pushed_chat_file(pushed_dir: Path, chat_id: str) -> Path:
    """Global per-target-chat dedup ledger path.

    Keyed by the chat the media is pushed to — the channel that would
    receive a duplicate — not by which subscriber's scan happened to send it.
    """
    pushed_dir.mkdir(parents=True, exist_ok=True)
    return pushed_dir / f"pushed_chat_{_safe_chat_token(chat_id)}.json"


def load_pushed_ids(pushed_dir: Path, tg_id: str, username: str) -> set[int]:
    """Load the per-(tg_id, creator) pushed ledger — FAIL-CLOSED.

    Missing file → empty set. An existing but unreadable/corrupt file raises
    ``StateReadError``: assuming empty would let the next scan re-push every
    historical ID.
    """
    return _read_int_set_strict(pushed_file_for_user(pushed_dir, tg_id, username))


def load_chat_pushed_ids(pushed_dir: Path, chat_id: str) -> set[int]:
    """Load the global per-chat pushed ledger — FAIL-CLOSED.

    On first use (no chat ledger file yet) the historical IDs of every
    ``pushed_ids_*.json`` in ``pushed_dir`` are UNIONED into the chat ledger
    so an upgraded install never re-pushes its history on the first scan.
    Corrupt legacy per-user files are skipped with a warning during this
    one-off merge; a corrupt chat ledger file itself raises StateReadError.
    """
    path = pushed_chat_file(pushed_dir, chat_id)
    if path.exists():
        return _read_int_set_strict(path)
    merged: set[int] = set()
    for legacy in sorted(pushed_dir.glob("pushed_ids_*.json")):
        try:
            raw = json.loads(legacy.read_text())
            if isinstance(raw, list):
                merged.update(int(v) for v in raw)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            log.warning(
                "Skipping unreadable legacy ledger %s during first-use "
                "chat-ledger merge: %s", legacy.name, e,
            )
    if merged:
        try:
            _atomic_write(path, json.dumps(sorted(merged), indent=2))
        except StateWriteError as e:
            log.warning("Could not persist merged chat ledger %s: %s", path, e)
        else:
            log.info(
                "Migrated %d historical pushed IDs into the global chat ledger %s",
                len(merged), path.name,
            )
    return merged


def save_pushed_ids(pushed_dir: Path, tg_id: str, username: str, ids: set[int]) -> None:
    """Persist pushed IDs, merging with any on-disk set under the lock.

    Merge-on-write avoids clobbering IDs saved by a concurrent writer. The
    caller's ``ids`` set is updated in place to the merged result.

    Raises ``StateReadError`` when the existing file is corrupt (fail-closed:
    never rewrite a ledger from partial in-memory state) and
    ``StateWriteError`` on lock timeout — callers must NOT treat the item
    as pushed in either case.
    """
    path = pushed_file_for_user(pushed_dir, tg_id, username)
    lock_path = _save_lock_path(pushed_dir, name="pushed")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                on_disk = _read_int_set_strict(path)
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
# Strict at-most-once claim primitives (single shared .pushed.lock)
# ---------------------------------------------------------------------------


def claim_push_slot(
    pushed_dir: Path,
    chat_id: str,
    tg_id: str,
    username: str,
    item_id: int,
) -> bool:
    """Atomically claim ``item_id`` for ``chat_id`` under .pushed.lock.

    Checks BOTH the global chat ledger and the per-(tg_id, username) ledger
    inside the same critical section: if either already contains ``item_id``
    the claim is refused (``False`` — skip the send entirely, 0 POSTs).
    Otherwise the ID is written to BOTH ledgers (chat ledger first, then the
    per-user one, rolling the chat entry back if the second write fails) and
    ``True`` is returned — the caller may now make its single send attempt.

    Fail-closed: ``StateReadError`` when any existing ledger is corrupt;
    ``StateWriteError`` on lock timeout or disk failure. Never swallows.
    """
    chat_path = pushed_chat_file(pushed_dir, chat_id)
    user_path = pushed_file_for_user(pushed_dir, tg_id, username)
    lock_path = _save_lock_path(pushed_dir, name="pushed")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                chat_ids = _read_int_set_strict(chat_path)
                user_ids = _read_int_set_strict(user_path)
                if item_id in chat_ids or item_id in user_ids:
                    return False
                _atomic_write(
                    chat_path, json.dumps(sorted(chat_ids | {item_id}), indent=2)
                )
                try:
                    _atomic_write(
                        user_path, json.dumps(sorted(user_ids | {item_id}), indent=2)
                    )
                except StateWriteError:
                    # Keep the two ledgers consistent: undo the chat claim so
                    # a retry (after the write error heals) is not refused.
                    try:
                        _atomic_write(
                            chat_path, json.dumps(sorted(chat_ids), indent=2)
                        )
                    except StateWriteError as revert_err:
                        log.error(
                            "Could not revert chat-ledger claim for id=%s after "
                            "user-ledger write failure: %s", item_id, revert_err,
                        )
                    raise
                return True
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                raise StateWriteError(
                    f"Timeout claiming push slot id={item_id} for @{username} "
                    f"after 3 attempts (lock: {lock_path})"
                )


def rollback_push_slot(
    pushed_dir: Path,
    chat_id: str,
    tg_id: str,
    username: str,
    item_id: int,
) -> None:
    """Remove ``item_id`` from both ledgers under .pushed.lock.

    Delta-write only: reads each on-disk set, ``discard``es the ID and writes
    the remainder back — concurrent claims of OTHER IDs made after this
    item's claim are never clobbered. Used when the send provably never
    reached Telegram (connect failure, pre-flight 429 with no text notice).
    """
    chat_path = pushed_chat_file(pushed_dir, chat_id)
    user_path = pushed_file_for_user(pushed_dir, tg_id, username)
    lock_path = _save_lock_path(pushed_dir, name="pushed")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                for path in (chat_path, user_path):
                    on_disk = _read_int_set_strict(path)
                    if item_id not in on_disk:
                        continue
                    on_disk.discard(item_id)
                    _atomic_write(path, json.dumps(sorted(on_disk), indent=2))
                return
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                raise StateWriteError(
                    f"Timeout rolling back push slot id={item_id} for @{username} "
                    f"after 3 attempts (lock: {lock_path})"
                )


def absorb_legacy_push_states(
    pushed_dir: Path,
    chat_id: str,
    tg_id: str,
    username: str,
) -> None:
    """Zero-resend absorption of legacy inflight/pending state files.

    Upgrades from the pre-strict-at-most-once design leave
    ``inflight_push_{tg_id}_{user}.json`` / ``pending_push_{tg_id}_{user}.json``
    files behind. Every ID they contain is uncertain-or-worse; re-sending
    would duplicate pushes for items that were in fact delivered. Under the
    shared lock all IDs are merged into BOTH pushed ledgers and the legacy
    files are deleted, so the old state costs zero re-sends.
    """
    chat_path = pushed_chat_file(pushed_dir, chat_id)
    user_path = pushed_file_for_user(pushed_dir, tg_id, username)
    lock_path = _save_lock_path(pushed_dir, name="pushed")
    for attempt in range(3):
        try:
            with FileLock(str(lock_path), timeout=10):
                absorbed: set[int] = set()
                legacy_files = [
                    _push_state_file(pushed_dir, "inflight", tg_id, username),
                    _push_state_file(pushed_dir, "pending", tg_id, username),
                ]
                for lf in legacy_files:
                    if not lf.exists():
                        continue
                    try:
                        raw = json.loads(lf.read_text())
                        if isinstance(raw, dict):
                            for k in raw:
                                try:
                                    absorbed.add(int(k))
                                except (TypeError, ValueError):
                                    continue
                    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
                        log.warning(
                            "Could not parse legacy push state %s during "
                            "absorption (skipping its IDs, file kept): %s",
                            lf.name, e,
                        )
                if absorbed:
                    chat_ids = _read_int_set_strict(chat_path)
                    user_ids = _read_int_set_strict(user_path)
                    _atomic_write(
                        chat_path,
                        json.dumps(sorted(chat_ids | absorbed), indent=2),
                    )
                    _atomic_write(
                        user_path,
                        json.dumps(sorted(user_ids | absorbed), indent=2),
                    )
                    log.warning(
                        "Absorbed %d legacy inflight/pending ID(s) into the "
                        "pushed ledgers for @%s (zero-resend upgrade)",
                        len(absorbed), username,
                    )
                for lf in legacy_files:
                    if lf.exists():
                        try:
                            lf.unlink()
                        except OSError as e:
                            log.warning(
                                "Could not remove absorbed legacy state file %s: %s",
                                lf.name, e,
                            )
                return
        except Timeout:
            if attempt < 2:
                time.sleep(2)
            else:
                raise StateWriteError(
                    f"Timeout absorbing legacy push state for @{username} "
                    f"after 3 attempts (lock: {lock_path})"
                )


# ---------------------------------------------------------------------------
# Push history: durable id -> unix-ts log of successful pushes.
#
# This is the data source for the bot's weekly activity report (per-
# subscription push count over the last 7 days + last push date). Entries
# are recorded ONLY for DELIVERED_OK sends — the report counts real
# deliveries, never claims.
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

    Corrupt / unreadable / missing file -> empty dict; the weekly report
    treats empty as "no push on record".
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


# ---------------------------------------------------------------------------
# Legacy lifecycle helpers retained as thin no-ops / read-only shims so that
# pre-strict-at-most-once callers keep importing. All writes funnel into the
# update_push_timestamps / update_pending_map merge primitives.
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
    """Record uncertain delivery (legacy API; absorbed into pushed on upgrade)."""
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

    Retained for legacy compatibility; strict at-most-once callers use
    ``absorb_legacy_push_states`` instead, which folds these IDs directly
    into the pushed ledgers (zero-resend).
    """
    inflight = load_push_timestamps(state_dir, "inflight", tg_id, username)
    if not inflight:
        return load_pending_map(state_dir, tg_id, username)
    log.warning(
        "Adopting %d leftover inflight ID(s) as pending for @%s (crash recovery)",
        len(inflight), username,
    )
    existing = load_pending_map(state_dir, tg_id, username)
    add: PendingMap = {}
    for iid, ts in inflight.items():
        if iid in existing:
            prev_ts, retries, mf = existing[iid]
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
