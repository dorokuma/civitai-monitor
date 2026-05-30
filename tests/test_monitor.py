"""Tests for monitor.py — nsfw_tracks, seen_ids, cleanup_old_caches, atomic writes."""

import os
import tempfile
import time
from pathlib import Path

import pytest

from monitor import (
    cleanup_old_caches,
    load_pushed_ids,
    load_seen_ids,
    nsfw_tracks,
    pushed_file_for_user,
    save_seen_ids,
    seen_file_for_user,
)


# ---------------------------------------------------------------------------
# nsfw_tracks
# ---------------------------------------------------------------------------

class TestNsfwTracks:
    def test_sfw_only(self):
        assert nsfw_tracks("sfw_only") == [False]

    def test_nsfw_only(self):
        assert nsfw_tracks("nsfw_only") == [True]

    def test_both(self):
        assert nsfw_tracks("both") == [False, True]

    def test_unknown_defaults_to_both(self):
        result = nsfw_tracks("garbage")
        assert result == [False, True]


# ---------------------------------------------------------------------------
# seen_ids / pushed_ids helpers (with tempfile isolation)
# ---------------------------------------------------------------------------

class TestSeenIdsHelpers:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_seen_file_for_user_path(self, tmp_dir):
        path = seen_file_for_user(tmp_dir, "123", "alice")
        assert path.name == "seen_ids_123_alice.json"

    def test_load_seen_ids_empty(self, tmp_dir):
        ids = load_seen_ids(tmp_dir, "123", "nobody")
        assert ids == set()

    def test_save_and_load_seen_ids(self, tmp_dir):
        save_seen_ids(tmp_dir, "123", "alice", {10, 20, 30})
        ids = load_seen_ids(tmp_dir, "123", "alice")
        assert ids == {10, 20, 30}

    def test_seen_ids_atomic_write(self, tmp_dir):
        """save writes to .tmp then renames; after save the main file exists."""
        path = seen_file_for_user(tmp_dir, "123", "alice")
        save_seen_ids(tmp_dir, "123", "alice", {1, 2, 3})
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()

    def test_pushed_file_for_user_path(self, tmp_dir):
        path = pushed_file_for_user(tmp_dir, "123", "alice")
        assert path.name == "pushed_ids_123_alice.json"

    def test_load_pushed_ids_empty(self, tmp_dir):
        ids = load_pushed_ids(tmp_dir, "123", "nobody")
        assert ids == set()


# ---------------------------------------------------------------------------
# cleanup_old_caches
# ---------------------------------------------------------------------------

class TestCleanupOldCaches:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def _touch_old(self, path: Path, days_ago: int) -> None:
        """Create a file and set its mtime to `days_ago` days in the past."""
        path.touch()
        mtime = time.time() - days_ago * 86400
        os.utime(path, (mtime, mtime))

    def test_cleanup_by_days_deletes_old_files(self, tmp_dir):
        old_file = tmp_dir / "old.txt"
        self._touch_old(old_file, days_ago=10)
        removed = cleanup_old_caches(tmp_dir, keep_days=7, max_total_gb=0)
        assert not old_file.exists()
        assert removed == 1

    def test_cleanup_preserves_recent_files(self, tmp_dir):
        recent = tmp_dir / "recent.txt"
        recent.touch()
        removed = cleanup_old_caches(tmp_dir, keep_days=7, max_total_gb=0)
        assert recent.exists()
        assert removed == 0

    def test_cleanup_nonexistent_dir_returns_zero(self, tmp_dir):
        nonexistent = tmp_dir / "does_not_exist"
        assert cleanup_old_caches(nonexistent, keep_days=7) == 0

    def test_cleanup_keeps_zero_days_preserves_files(self, tmp_dir):
        """keep_days=0 skips date-based cleanup entirely (files are preserved)."""
        f = tmp_dir / "any.txt"
        f.touch()
        removed = cleanup_old_caches(tmp_dir, keep_days=0, max_total_gb=0)
        assert f.exists()  # 0 means "don't touch date-based cleanup"
        assert removed == 0
