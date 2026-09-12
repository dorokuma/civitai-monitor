"""Tests for state_store.py — OSError (e.g. disk full / ENOSPC) handling.

``_atomic_write`` must wrap any OSError into ``StateWriteError`` — the same
contract as the pre-existing FileLock ``Timeout`` path — because all write
entry points only catch ``Timeout`` and their callers (_process_single_creator
& co.) only catch ``StateWriteError``. Without the wrapper a raw OSError
escapes and crashes the whole monitor process, silently skipping every later
subscriber. A failed write must also not leave a ``*.tmp`` file behind.
"""

import errno
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from filelock import Timeout

import state_store
from state_store import (
    StateWriteError,
    _atomic_write,
    pushed_file_for_user,
    save_pushed_ids,
    save_seen_ids,
    seen_file_for_user,
    update_pending_map,
)


def _enospc(*_args, **_kwargs):
    """Simulate a disk-full error (ENOSPC) for os.fsync / Path.replace."""
    raise OSError(errno.ENOSPC, "No space left on device")


def _no_tmp_files(tmp_path: Path) -> bool:
    """True if no leftover *.tmp files exist in tmp_path."""
    return list(tmp_path.glob("*.tmp")) == []


def target_mode(path: Path) -> int:
    """Permission bits of *path*."""
    return path.stat().st_mode & 0o777


# ---------------------------------------------------------------------------
# t1: OSError (ENOSPC) during the write path → StateWriteError, no tmp leftover
# ---------------------------------------------------------------------------


class TestAtomicWriteOSError:
    """OSError raised inside _atomic_write surfaces as StateWriteError."""

    def test_fsync_enospc_raises_state_write_error(self, tmp_path, monkeypatch):
        """os.fsync failure (disk full) → StateWriteError naming the cause."""
        target = tmp_path / "seen_ids_tg1_alice.json"
        monkeypatch.setattr(os, "fsync", _enospc)

        with pytest.raises(StateWriteError) as excinfo:
            _atomic_write(target, "[1, 2, 3]")

        msg = str(excinfo.value)
        assert "OSError" in msg
        assert "No space left on device" in msg
        assert str(errno.ENOSPC) in msg  # errno 28 is part of the message
        # the half-written tmp file must be cleaned up
        assert _no_tmp_files(tmp_path)
        assert not target.exists()

    def test_replace_enospc_raises_state_write_error(self, tmp_path, monkeypatch):
        """Path.replace failure → StateWriteError and no tmp residue."""
        target = tmp_path / "state.json"
        monkeypatch.setattr(Path, "replace", _enospc)

        with pytest.raises(StateWriteError) as excinfo:
            _atomic_write(target, "[1]")

        assert "No space left on device" in str(excinfo.value)
        assert _no_tmp_files(tmp_path)

    def test_open_enospc_raises_state_write_error(self, tmp_path, monkeypatch):
        """open() of the tmp file failing (never created) → StateWriteError.

        Exercises the missing_ok=True cleanup: the tmp file does not exist,
        so unlinking must not raise a secondary error.
        """
        target = tmp_path / "state.json"
        real_open = open

        def fake_open(file, *args, **kwargs):
            if str(file).endswith(".tmp"):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)

        with pytest.raises(StateWriteError) as excinfo:
            _atomic_write(target, "[1]")

        assert "No space left on device" in str(excinfo.value)
        assert _no_tmp_files(tmp_path)


class TestWriteEntryPointsPropagate:
    """All write entry points must let the OSError-derived StateWriteError
    propagate (their ``except Timeout`` must stay transparent to it)."""

    def test_save_seen_ids_enospc_raises_state_write_error(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(os, "fsync", _enospc)
        with pytest.raises(StateWriteError):
            save_seen_ids(tmp_path, "tg1", "alice", {1, 2, 3})
        assert _no_tmp_files(tmp_path)
        assert not seen_file_for_user(tmp_path, "tg1", "alice").exists()

    def test_save_pushed_ids_enospc_keeps_in_memory_state(
        self, tmp_path, monkeypatch
    ):
        """Merge-on-write rollback untouched: caller's set is not mutated."""
        monkeypatch.setattr(os, "fsync", _enospc)
        ids = {7}
        with pytest.raises(StateWriteError):
            save_pushed_ids(tmp_path, "tg1", "bob", ids)
        assert ids == {7}
        assert _no_tmp_files(tmp_path)
        assert not pushed_file_for_user(tmp_path, "tg1", "bob").exists()

    def test_update_pending_map_enospc_raises_state_write_error(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(os, "fsync", _enospc)
        with pytest.raises(StateWriteError):
            update_pending_map(tmp_path, "tg1", "alice", add={5: (123.0, 0)})
        assert _no_tmp_files(tmp_path)


# ---------------------------------------------------------------------------
# t2: normal write regression — roundtrip consistency + 0600 mode
# ---------------------------------------------------------------------------


class TestNormalWriteRegression:
    def test_atomic_write_roundtrip_and_mode_0600(self, tmp_path):
        target = tmp_path / "state.json"
        _atomic_write(target, json.dumps({"k": [1, 2]}))
        assert json.loads(target.read_text()) == {"k": [1, 2]}
        assert not target.with_suffix(".json.tmp").exists()
        assert target_mode(target) == 0o600

    def test_save_seen_ids_roundtrip_and_mode_0600(self, tmp_path):
        save_seen_ids(tmp_path, "tg1", "alice", {3, 1, 2})
        path = seen_file_for_user(tmp_path, "tg1", "alice")
        assert json.loads(path.read_text()) == [1, 2, 3]
        assert target_mode(path) == 0o600

    def test_save_pushed_ids_roundtrip_and_mode_0600(self, tmp_path):
        save_pushed_ids(tmp_path, "tg1", "bob", {5})
        path = pushed_file_for_user(tmp_path, "tg1", "bob")
        assert json.loads(path.read_text()) == [5]
        assert target_mode(path) == 0o600


# ---------------------------------------------------------------------------
# t3: FileLock Timeout → StateWriteError (pre-existing contract) unchanged
# ---------------------------------------------------------------------------


class TestTimeoutBehaviourRegression:
    def test_save_seen_ids_lock_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state_store.time, "sleep", lambda *_: None)
        with patch("state_store.FileLock") as mock_lock:
            mock_lock.side_effect = Timeout(str(tmp_path / ".seen.lock"))
            with pytest.raises(StateWriteError) as excinfo:
                save_seen_ids(tmp_path, "tg1", "alice", {1, 2})
        assert "Timeout" in str(excinfo.value)
        assert "after 3 attempts" in str(excinfo.value)

    def test_save_pushed_ids_lock_timeout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(state_store.time, "sleep", lambda *_: None)
        with patch("state_store.FileLock") as mock_lock:
            mock_lock.side_effect = Timeout(str(tmp_path / ".pushed.lock"))
            with pytest.raises(StateWriteError):
                save_pushed_ids(tmp_path, "tg1", "bob", {1})
