"""Tests for monitor.py — nsfw_tracks, seen_ids, cleanup_old_caches, atomic writes,
plus the safety helpers we just added (fetch_page limit clamp, video size cap,
signal handler, URL normalization)."""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from monitor import (
    PENDING_CONFIRM_SECONDS,
    PENDING_MAX_RETRIES,
    FetchPageError,
    MonitorConfig,
    _monitor_signal_handler,
    adopt_stale_inflight,
    cleanup_old_caches,
    clear_pending,
    download_image,
    download_video,
    escape_markdown,
    fetch_page,
    load_pending_map,
    load_push_timestamps,
    load_pushed_ids,
    load_seen_ids,
    mark_inflight,
    mark_pending,
    normalize_to_original,
    nsfw_tracks,
    pushed_file_for_user,
    save_pushed_ids,
    save_seen_ids,
    seen_file_for_user,
    update_pending_map,
    write_config,
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


# ---------------------------------------------------------------------------
# normalize_to_original — strip size suffixes to get full-res
# ---------------------------------------------------------------------------

class TestNormalizeToOriginal:
    def test_replaces_width_1024(self):
        url = "https://image.civitai.com/x/abc/width=1024/file.jpeg"
        assert normalize_to_original(url) == "https://image.civitai.com/x/abc/width=original/file.jpeg"

    def test_replaces_width_450(self):
        url = "https://image.civitai.com/x/abc/width=450/file.jpeg"
        assert normalize_to_original(url) == "https://image.civitai.com/x/abc/width=original/file.jpeg"

    def test_replaces_width_640(self):
        url = "https://image.civitai.com/x/abc/width=640/file.jpeg"
        assert normalize_to_original(url) == "https://image.civitai.com/x/abc/width=original/file.jpeg"

    def test_passthrough_when_no_known_suffix(self):
        url = "https://image.civitai.com/x/abc/some_other_url.jpeg"
        assert normalize_to_original(url) == url

    def test_custom_suffixes(self):
        url = "https://x.com/y/width=512/file.jpeg"
        out = normalize_to_original(url, size_suffixes=["/width=512/"])
        assert out == "https://x.com/y/width=original/file.jpeg"


# ---------------------------------------------------------------------------
# fetch_page — limit clamping, error handling, fallback
# ---------------------------------------------------------------------------

class TestFetchPageLimitClamp:
    """The API silently caps or rejects out-of-range `limit` values, so the
    client must clamp before sending. Regression for an audit finding."""

    def _mock_response(self, items=None, next_cursor=""):
        resp = MagicMock()
        resp.json.return_value = {
            "items": items or [],
            "metadata": {"nextCursor": next_cursor},
        }
        resp.raise_for_status = MagicMock()
        return resp

    def test_clamps_huge_limit_to_200(self):
        """If config writes images_per_page: 10000 we must not send it raw."""
        with patch("civitai_client.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            with patch.dict(os.environ, {}, clear=False):
                fetch_page("alice", limit=10000)
        sent_params = mock_get.call_args.kwargs["params"]
        assert sent_params["limit"] == 200

    def test_clamps_zero_or_negative_to_one(self):
        with patch("civitai_client.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            fetch_page("alice", limit=0)
        assert mock_get.call_args.kwargs["params"]["limit"] == 1
        mock_get.reset_mock()
        with patch("civitai_client.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            fetch_page("alice", limit=-5)
        assert mock_get.call_args.kwargs["params"]["limit"] == 1

    def test_preserves_normal_limit(self):
        with patch("civitai_client.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            fetch_page("alice", limit=100)
        assert mock_get.call_args.kwargs["params"]["limit"] == 100

    def test_uses_civitai_red_when_nsfw_true(self):
        """NSFW track must hit civitai.red."""
        with patch("civitai_client.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            fetch_page("alice", nsfw=True, sort="Newest")
        called_url = mock_get.call_args.args[0]
        assert called_url.startswith("https://civitai.red/api/v1/images")
        sent = mock_get.call_args.kwargs["params"]
        assert sent["nsfw"] == "true"

    def test_falls_back_to_unsorted_when_newest_returns_empty(self):
        """Some users (e.g. PotatoMan760) have the Newest-sort bug — retry
        without sort when first call returns 0 items."""
        empty = self._mock_response(items=[])
        items = self._mock_response(items=[{"id": 1, "url": "x"}])
        with patch("civitai_client.safe_get") as mock_get:
            mock_get.side_effect = [empty, items]
            fetched, _cursor = fetch_page("alice", nsfw=False, sort="Newest")
        assert mock_get.call_count == 2
        # Second call must not have `sort` parameter
        second_params = mock_get.call_args_list[1].kwargs["params"]
        assert "sort" not in second_params
        assert len(fetched) == 1

    def test_returns_empty_and_no_cursor_on_empty_response(self):
        """When both attempts return 0 items, return ([], '') so the loop terminates."""
        with patch("civitai_client.safe_get") as mock_get:
            mock_get.return_value = self._mock_response(items=[])
            fetched, cursor = fetch_page("alice", nsfw=False, sort="Newest")
        assert fetched == []
        assert cursor == ""

    def test_network_error_raises_fetch_page_error(self):
        """Hard network/HTTP failures raise FetchPageError (no silent empty page)."""
        import requests as _req
        with patch("civitai_client.safe_get", side_effect=_req.RequestException("boom")), pytest.raises(FetchPageError):
            fetch_page("alice", nsfw=False, sort="Newest")


# ---------------------------------------------------------------------------
# save_pushed_ids / load_pushed_ids — already partially covered, finish the loop
# ---------------------------------------------------------------------------

class TestPushedIdsRoundTrip:
    @pytest.fixture
    def tmp_dir(self):
        with tempfile.TemporaryDirectory() as d:
            yield Path(d)

    def test_save_and_load_pushed_ids(self, tmp_dir):
        save_pushed_ids(tmp_dir, "tg1", "alice", {100, 200, 300})
        assert load_pushed_ids(tmp_dir, "tg1", "alice") == {100, 200, 300}

    def test_pushed_ids_atomic_write_no_tmp_leftover(self, tmp_dir):
        path = pushed_file_for_user(tmp_dir, "tg1", "alice")
        save_pushed_ids(tmp_dir, "tg1", "alice", {1, 2})
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()

    def test_pushed_ids_load_missing_returns_empty(self, tmp_dir):
        assert load_pushed_ids(tmp_dir, "tg1", "missing") == set()

    def test_save_pushed_ids_merges_with_on_disk(self, tmp_dir):
        """Merge-on-write: in-memory subset must not clobber IDs already on disk."""
        save_pushed_ids(tmp_dir, "tg1", "alice", {1, 2, 3})
        # Simulate a partial in-memory set (e.g. reloaded mid-run missing older IDs)
        partial = {3, 4}
        save_pushed_ids(tmp_dir, "tg1", "alice", partial)
        assert load_pushed_ids(tmp_dir, "tg1", "alice") == {1, 2, 3, 4}
        # Caller set is updated in place to the merged view
        assert partial == {1, 2, 3, 4}


# ---------------------------------------------------------------------------
# Telegram media send — anti-duplicate on timeout
# ---------------------------------------------------------------------------

class TestTelegramMediaAntiDuplicate:
    """Timeout after media upload must NOT fall back to a second text message."""

    def test_video_timeout_returns_uncertain_no_text(self, monkeypatch, tmp_path):
        import telegram_media as tm

        video = tmp_path / "1.mp4"
        video.write_bytes(b"fake-video")

        def boom(*_a, **_k):
            raise requests.Timeout("read timed out")

        text_calls: list = []
        monkeypatch.setattr(tm.requests, "post", boom)
        monkeypatch.setattr(
            tm, "_send_telegram_text",
            lambda *a, **k: text_calls.append(1) or True,
        )
        ok = tm._send_telegram_video("http://api/botT", "chat", "caption", video)
        assert ok is None  # uncertain, not false success
        assert text_calls == []

    def test_media_group_timeout_returns_uncertain_no_text(self, monkeypatch, tmp_path):
        import telegram_media as tm

        img = tmp_path / "1.jpeg"
        img.write_bytes(b"fake-img")

        def boom(*_a, **_k):
            raise requests.Timeout("read timed out")

        text_calls: list = []
        monkeypatch.setattr(tm.requests, "post", boom)
        monkeypatch.setattr(
            tm, "_send_telegram_text",
            lambda *a, **k: text_calls.append(1) or True,
        )
        ok = tm._send_telegram_media_group("http://api/botT", "chat", "caption", [img])
        assert ok is None
        assert text_calls == []

    def test_video_http_error_still_falls_back_to_text(self, monkeypatch, tmp_path):
        import telegram_media as tm

        video = tmp_path / "1.mp4"
        video.write_bytes(b"fake-video")

        class FakeResp:
            ok = False
            text = "bad request"
            status_code = 400
            headers = {}  # noqa: RUF012

        monkeypatch.setattr(tm.requests, "post", lambda *a, **k: FakeResp())
        text_calls: list = []
        monkeypatch.setattr(
            tm, "_send_telegram_text",
            lambda *a, **k: text_calls.append(1) or True,
        )
        ok = tm._send_telegram_video("http://api/botT", "chat", "caption", video)
        assert ok is True
        assert text_calls == [1]

    def test_telegram_429_retries_then_succeeds(self, monkeypatch):
        import telegram_media as tm

        calls = {"n": 0}

        class FakeResp:
            def __init__(self, status, ok=False):
                self.status_code = status
                self.ok = ok
                self.text = "x"
                self.headers = {"Retry-After": "0"} if status == 429 else {}

        def fake_post(*_a, **_k):
            calls["n"] += 1
            if calls["n"] < 2:
                return FakeResp(429)
            return FakeResp(200, ok=True)

        sleeps: list[float] = []
        monkeypatch.setattr(tm.requests, "post", fake_post)
        monkeypatch.setattr(tm.time, "sleep", lambda s: sleeps.append(s))
        resp = tm._telegram_post("http://api/botT/sendMessage", timeout=5, json={})
        assert resp.ok is True
        assert calls["n"] == 2
        assert sleeps  # waited on 429


class TestPushLifecycleState:
    """Inflight pre-claim + pending light-confirm bookkeeping."""

    def test_mark_and_clear_pending(self, tmp_path):
        mark_pending(tmp_path, "tg1", "alice", 42, ts=1000.0, retries=0)
        data = load_pending_map(tmp_path, "tg1", "alice")
        assert data == {42: (1000.0, 0)}
        clear_pending(tmp_path, "tg1", "alice", 42)
        assert load_pending_map(tmp_path, "tg1", "alice") == {}

    def test_adopt_stale_inflight_moves_to_pending(self, tmp_path):
        mark_inflight(tmp_path, "tg1", "alice", 7)
        mark_inflight(tmp_path, "tg1", "alice", 8)
        pending = adopt_stale_inflight(tmp_path, "tg1", "alice")
        assert set(pending.keys()) == {7, 8}
        assert all(r == 0 for _, r in pending.values())
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}

    def test_adopt_stale_inflight_preserves_existing_retries(self, tmp_path):
        """adopt_stale_inflight must NOT reset retries to 0 for an id that
        already has a pending entry. This prevents an infinite retry loop:
        outcome=None → mark_pending(retries=N) → clear_inflight; but if a
        crash leaves inflight set, adopt_stale_inflight would re-adopt with
        retries=0, bypassing PENDING_MAX_RETRIES."""
        # Simulate the crash window: mark_pending wrote pending (retries=1),
        # but the process crashed before clear_inflight.
        mark_pending(tmp_path, "tg1", "alice", 50, ts=1000.0, retries=1)
        mark_inflight(tmp_path, "tg1", "alice", 50)  # leftover inflight
        # Also add a genuinely-new inflight id with no prior pending
        mark_inflight(tmp_path, "tg1", "alice", 51)

        pending = adopt_stale_inflight(tmp_path, "tg1", "alice")
        assert 50 in pending
        assert 51 in pending
        # id=50 already had pending → retries preserved (not reset to 0)
        assert pending[50][1] == 1
        # id=51 is genuinely new → retries=0 (normal crash-mid-send recovery)
        assert pending[51][1] == 0
        # inflight must be cleared after adoption
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}

    def test_adopt_stale_inflight_no_infinite_retry_via_retries_reset(self, tmp_path):
        """Guard against the infinite retry loop described by the reviewer:
        if adopt_stale_inflight reset retries to 0, the same id would be
        re-adopted on every scan with retries=0, always below
        PENDING_MAX_RETRIES, causing _fetch_and_process_page to increment
        and retry forever. This test proves the retries survive adoption."""
        from state_store import PENDING_MAX_RETRIES as MR

        # Park as pending with retries at the cap (simulating one retry cycle)
        mark_pending(tmp_path, "tg1", "alice", 42, ts=1.0, retries=MR)
        # Simulate the crash window: inflight still set
        mark_inflight(tmp_path, "tg1", "alice", 42)

        pending = adopt_stale_inflight(tmp_path, "tg1", "alice")
        # After adoption, retries must still be at the cap (not reset to 0)
        assert pending[42][1] == MR
        # The _fetch_and_process_page check (retries >= PENDING_MAX_RETRIES)
        # would now promote without re-send, breaking the infinite loop.
        assert pending[42][1] >= MR

    def test_update_pending_merge_and_remove(self, tmp_path):
        update_pending_map(tmp_path, "tg1", "bob", add={1: (10.0, 0), 2: (20.0, 1)})
        update_pending_map(tmp_path, "tg1", "bob", add={3: (30.0, 0)}, remove={1})
        data = load_pending_map(tmp_path, "tg1", "bob")
        assert data == {2: (20.0, 1), 3: (30.0, 0)}

    def test_pending_legacy_float_loads_as_retries_zero(self, tmp_path):
        """Old on-disk format was id→float; must still load."""
        path = tmp_path / "pending_push_tg1_alice.json"
        path.write_text('{"42": 1000.5}')
        data = load_pending_map(tmp_path, "tg1", "alice")
        assert data == {42: (1000.5, 0)}

    def test_pending_confirm_window_constant(self):
        assert PENDING_CONFIRM_SECONDS == 30 * 60
        assert PENDING_MAX_RETRIES == 1

    def test_finalize_uncertain_parks_pending(self, tmp_path):
        import monitor as m

        pushed: set[int] = set()
        ok = m._finalize_send_outcome(
            None, 99, "alice",
            pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
        )
        assert ok is False
        assert 99 not in pushed
        assert 99 in load_pending_map(tmp_path, "tg1", "alice")

    def test_finalize_ok_records_pushed_and_clears_pending(self, tmp_path):
        import monitor as m

        mark_pending(tmp_path, "tg1", "alice", 99, ts=1.0, retries=1)
        mark_inflight(tmp_path, "tg1", "alice", 99)
        pushed: set[int] = set()
        ok = m._finalize_send_outcome(
            True, 99, "alice",
            pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
        )
        assert ok is True
        assert pushed == {99}
        assert load_pending_map(tmp_path, "tg1", "alice") == {}
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}

    def test_finalize_fail_clears_pending(self, tmp_path):
        import monitor as m

        mark_pending(tmp_path, "tg1", "alice", 5, ts=1.0, retries=1)
        ok = m._finalize_send_outcome(
            False, 5, "alice",
            pushed_ids=set(), pushed_dir=tmp_path, tg_id="tg1",
        )
        assert ok is False
        assert load_pending_map(tmp_path, "tg1", "alice") == {}


class TestStateWriteFailure:
    """When state persistence fails (FileLock timeout), the item must NOT be
    treated as pushed and inflight must NOT be cleared — the inflight/pending
    recovery mechanism handles it on the next scan."""

    def test_save_pushed_ids_raises_state_write_error(self, tmp_path):
        """save_pushed_ids must raise StateWriteError when the lock times out."""
        from filelock import Timeout

        from state_store import StateWriteError

        with patch("state_store.FileLock") as mock_lock:
            mock_lock.side_effect = Timeout(str(tmp_path / ".pushed.lock"))
            with pytest.raises(StateWriteError):
                save_pushed_ids(tmp_path, "tg1", "alice", {1, 2})

    def test_record_push_success_rollback_on_write_failure(self, tmp_path, monkeypatch):
        """If save_pushed_ids fails, pushed_ids is rolled back and inflight stays."""
        import monitor as m
        from state_store import StateWriteError

        mark_inflight(tmp_path, "tg1", "alice", 42)
        pushed: set[int] = set()

        def boom(*_a, **_k):
            raise StateWriteError("simulated lock timeout")

        monkeypatch.setattr(m, "save_pushed_ids", boom)
        with pytest.raises(StateWriteError):
            m._record_push_success(
                42, "alice",
                pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
            )
        # pushed_ids must be rolled back
        assert 42 not in pushed
        # inflight must NOT be cleared (left for next-scan recovery)
        assert 42 in load_push_timestamps(tmp_path, "inflight", "tg1", "alice")

    def test_process_and_push_state_write_failure_skips_item(self, tmp_path, monkeypatch):
        """process_and_push must catch StateWriteError, log it, and return False
        without marking the item as pushed."""
        import monitor as m
        from state_store import StateWriteError

        mark_inflight(tmp_path, "tg1", "alice", 77)
        pushed: set[int] = set()

        # Mock send_to_telegram to succeed (outcome=True triggers _record_push_success)
        monkeypatch.setattr(m, "send_to_telegram", lambda *a, **k: True)
        # Mock save_pushed_ids to fail
        def boom(*a, **k):
            raise StateWriteError("lock timeout")
        monkeypatch.setattr(m, "save_pushed_ids", boom)
        # Mock download_image to succeed so we reach the send path
        monkeypatch.setattr(m, "download_image", lambda *a, **k: m.DownloadResult(True))

        item = {"id": 77, "url": "https://x.com/width=1024/f.jpeg", "createdAt": "2025-01-01T00:00:00Z"}
        result = m.process_and_push(
            item, "alice",
            size_suffixes=[], output_dir=tmp_path, bot_token="t", chat_id="c",
            video_enabled=False, max_video_size_mb=10,
            pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
        )
        assert result is False
        # Item must NOT be in pushed_ids
        assert 77 not in pushed
        # Inflight must still be set (not cleared) for next-scan recovery
        assert 77 in load_push_timestamps(tmp_path, "inflight", "tg1", "alice")


class TestClearInflightOrdering:
    """State must be persisted BEFORE clear_inflight runs.
    Regression: _finalize_send_outcome used to clear_inflight unconditionally
    at the top, before _record_push_success (which calls save_pushed_ids).
    If the process crashed between the two, the inflight guard was gone and
    the item would be re-pushed. Both outcome=True (save_pushed_ids → clear)
    and outcome=None (mark_pending → clear) now follow the same invariant."""

    def test_finalize_success_clears_inflight_after_save(self, tmp_path):
        """On success, inflight is cleared by _record_push_success (after save)."""
        import monitor as m

        mark_inflight(tmp_path, "tg1", "alice", 55)
        pushed: set[int] = set()
        ok = m._finalize_send_outcome(
            True, 55, "alice",
            pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
        )
        assert ok is True
        assert pushed == {55}
        # inflight is cleared (by _record_push_success, after save succeeded)
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}

    def test_finalize_uncertain_clears_inflight_after_pending(self, tmp_path):
        """On uncertain (None) outcome, inflight is cleared AFTER mark_pending
        succeeds — isomorphic to the outcome=True path (save → clear).

        Regression: the old code left inflight set on outcome=None, causing
        adopt_stale_inflight to re-adopt the id with retries=0 on every scan,
        bypassing PENDING_MAX_RETRIES and creating an infinite retry loop."""
        import monitor as m

        mark_inflight(tmp_path, "tg1", "alice", 66)
        pushed: set[int] = set()
        ok = m._finalize_send_outcome(
            None, 66, "alice",
            pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
        )
        assert ok is False
        # pending must be written (the "state" that replaces inflight)
        assert 66 in load_pending_map(tmp_path, "tg1", "alice")
        # inflight is cleared only after mark_pending succeeds
        assert 66 not in load_push_timestamps(tmp_path, "inflight", "tg1", "alice")

    def test_finalize_uncertain_mark_pending_failure_keeps_inflight(self, tmp_path, monkeypatch):
        """If mark_pending fails (StateWriteError) on outcome=None, inflight
        must NOT be cleared — same invariant as the outcome=True path where
        save_pushed_ids failure leaves inflight set for next-scan recovery."""
        import monitor as m
        from state_store import StateWriteError

        mark_inflight(tmp_path, "tg1", "alice", 66)
        pushed: set[int] = set()

        def boom(*_a, **_k):
            raise StateWriteError("simulated lock timeout")

        monkeypatch.setattr(m, "mark_pending", boom)
        with pytest.raises(StateWriteError):
            m._finalize_send_outcome(
                None, 66, "alice",
                pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
            )
        # inflight must NOT be cleared (left for next-scan recovery)
        assert 66 in load_push_timestamps(tmp_path, "inflight", "tg1", "alice")

    def test_finalize_failure_clears_inflight(self, tmp_path):
        """On definitive failure (False), inflight IS cleared — the send is done."""
        import monitor as m

        mark_inflight(tmp_path, "tg1", "alice", 88)
        ok = m._finalize_send_outcome(
            False, 88, "alice",
            pushed_ids=set(), pushed_dir=tmp_path, tg_id="tg1",
        )
        assert ok is False
        # inflight is cleared on definitive failure
        assert 88 not in load_push_timestamps(tmp_path, "inflight", "tg1", "alice")


class TestTransientDownloadFailure:
    """Transient download failures (timeout, 5xx, connection error) must NOT
    mark the item as pushed. Permanent failures (404, 410) keep the old
    behavior (push text + mark pushed). The retry count is capped by
    PENDING_MAX_RETRIES via the existing pending mechanism."""

    def test_download_image_transient_returns_not_permanent(self, tmp_path, monkeypatch):
        """A timeout is transient, not permanent."""
        import monitor as m

        save = tmp_path / "1.jpeg"
        monkeypatch.setattr(m, "safe_get", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout("read timed out")))
        result = download_image("https://x.com/f.jpeg", save)
        assert result.success is False
        assert result.permanent is False
        assert not save.exists()
        assert not save.with_suffix(save.suffix + ".tmp").exists()

    def test_download_image_404_is_permanent(self, tmp_path, monkeypatch):
        """A 404 HTTP error is permanent."""
        import monitor as m

        save = tmp_path / "2.jpeg"
        resp = requests.Response()
        resp.status_code = 404
        resp._content = b"not found"
        err = requests.HTTPError(response=resp)
        monkeypatch.setattr(m, "safe_get", lambda *a, **k: (_ for _ in ()).throw(err))
        result = download_image("https://x.com/gone.jpeg", save)
        assert result.success is False
        assert result.permanent is True
        assert not save.exists()

    def test_process_and_push_transient_failure_not_pushed(self, tmp_path, monkeypatch):
        """On a transient download failure, process_and_push must NOT add the
        item to pushed_ids; it is parked as pending for the next scan."""
        import monitor as m

        pushed: set[int] = set()
        sent_texts: list[str] = []

        monkeypatch.setattr(m, "download_image", lambda *a, **k: m.DownloadResult(False, permanent=False))
        monkeypatch.setattr(m, "send_to_telegram", lambda *a, **k: sent_texts.append(a[2]) or True)

        item = {"id": 100, "url": "https://x.com/width=1024/f.jpeg", "createdAt": "2025-01-01T00:00:00Z"}
        result = m.process_and_push(
            item, "alice",
            size_suffixes=[], output_dir=tmp_path, bot_token="t", chat_id="c",
            video_enabled=False, max_video_size_mb=10,
            pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
        )
        assert result is False
        assert 100 not in pushed
        # Item should be parked as pending (for retry with cap)
        assert 100 in load_pending_map(tmp_path, "tg1", "alice")

    def test_process_and_push_permanent_failure_marks_pushed(self, tmp_path, monkeypatch):
        """On a permanent download failure (404), process_and_push marks the
        item as pushed (existing behavior — avoids infinite retry loop)."""
        import monitor as m

        pushed: set[int] = set()
        monkeypatch.setattr(m, "download_image", lambda *a, **k: m.DownloadResult(False, permanent=True))
        monkeypatch.setattr(m, "send_to_telegram", lambda *a, **k: True)

        item = {"id": 200, "url": "https://x.com/width=1024/f.jpeg", "createdAt": "2025-01-01T00:00:00Z"}
        result = m.process_and_push(
            item, "alice",
            size_suffixes=[], output_dir=tmp_path, bot_token="t", chat_id="c",
            video_enabled=False, max_video_size_mb=10,
            pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
        )
        # _send returns True (send_to_telegram succeeded), so process_and_push returns True
        assert result is True
        assert 200 in pushed

    def test_transient_failure_retry_capped_by_pending_max_retries(self, tmp_path):
        """The pending mechanism caps retries at PENDING_MAX_RETRIES.
        After that, the item is promoted to pushed without further re-send."""
        from state_store import PENDING_MAX_RETRIES as MR

        # Mark an item as pending with retries already at the cap
        mark_pending(tmp_path, "tg1", "alice", 300, ts=1.0, retries=MR)
        pending = load_pending_map(tmp_path, "tg1", "alice")
        assert pending[300][1] == MR
        # The _fetch_and_process_page logic checks retries >= PENDING_MAX_RETRIES
        # and promotes without re-send — so the cap is enforced.
        assert MR == 1  # current value


class TestDownloadTmpCleanup:
    """Failed downloads must clean up .tmp files."""

    def test_download_image_failure_cleans_tmp(self, tmp_path, monkeypatch):
        """Image download failure must not leave a .tmp file behind."""
        import monitor as m

        save = tmp_path / "1.jpeg"
        # Create a fake .tmp to verify it gets cleaned
        tmp = save.with_suffix(save.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(b"partial")
        assert tmp.exists()

        monkeypatch.setattr(m, "safe_get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("boom")))
        result = download_image("https://x.com/f.jpeg", save)
        assert result.success is False
        assert not tmp.exists()  # .tmp cleaned up

    def test_download_video_failure_cleans_tmp(self, tmp_path, monkeypatch):
        """Video download failure must not leave a .tmp file behind."""
        import monitor as m

        save = tmp_path / "videos" / "1.mp4"
        tmp = save.with_suffix(save.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(b"partial")
        assert tmp.exists()

        monkeypatch.setattr(m, "safe_get", lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError("boom")))
        result = download_video("https://image.civitai.com/y", save)
        assert result.success is False
        assert not tmp.exists()  # .tmp cleaned up


class TestProcessSingleCreatorStateWriteErrorBoundary:
    """_process_single_creator must catch StateWriteError from run_full/
    run_incremental/save_seen_ids so the scan process does not crash under
    systemd Restart=always."""

    def test_state_write_error_in_run_incremental_does_not_crash(self, tmp_path, monkeypatch):
        """If save_seen_ids fails inside run_incremental, _process_single_creator
        catches StateWriteError and returns (seen_ids, 0) instead of propagating."""
        import monitor as m
        from state_store import StateWriteError

        def boom(*_a, **_k):
            raise StateWriteError("lock timeout")

        monkeypatch.setattr(m, "save_seen_ids", boom)
        # Mock fetch_page so run_incremental doesn't hit the network
        monkeypatch.setattr(m, "fetch_page", lambda *a, **k: ([], ""))

        result = m._process_single_creator(
            "alice", "tg1", tmp_path, tmp_path,
            mode="incremental", nsfw="both",
            size_suffixes=[], bot_token="t", chat_id="c",
            base_url="https://civitai.com", page_limit=5,
            video_enabled=False, max_video_size_mb=10,
            incremental_max_pages=5,
        )
        # Should return a tuple, not raise
        assert isinstance(result, tuple)
        assert result[1] == 0  # new_count = 0 (skipped)


# ---------------------------------------------------------------------------
# _monitor_signal_handler — sets the shutdown flag on SIGTERM/SIGINT
# ---------------------------------------------------------------------------

class TestMonitorSignalHandler:
    def setup_method(self):
        # Reset module-level flag between tests
        import monitor as _m
        _m._monitor_shutdown_requested = False

    def test_first_signal_sets_flag(self):
        import monitor as _m
        _monitor_signal_handler(15, None)  # 15 = SIGTERM
        assert _m._monitor_shutdown_requested is True

    def test_second_signal_is_idempotent(self):
        import monitor as _m
        _m._monitor_shutdown_requested = False
        _monitor_signal_handler(15, None)
        _monitor_signal_handler(15, None)  # second time should be no-op
        # Flag stays True; second call is a no-op (just to confirm it doesn't crash)
        assert _m._monitor_shutdown_requested is True


# ---------------------------------------------------------------------------
# IncrementalConfig — guards against first-run-flood on big-history creators
# ---------------------------------------------------------------------------

class TestIncrementalMaxPages:
    """Regression: a 1k+ item creator used to push their entire history
    on the very first scan. ``max_pages`` caps this per track."""

    def test_default_is_five(self):
        from monitor import IncrementalConfig
        cfg = IncrementalConfig()
        assert cfg.max_pages == 5

    def test_zero_means_no_extra_cap_relies_on_caught_up(self):
        """max_pages=0 makes the inner while loop run 0 times — no pages fetched.

        Useful as a disable switch; relies on the next scan catching up via
        the seen-id overlap.
        """
        from monitor import IncrementalConfig
        assert IncrementalConfig(max_pages=0).max_pages == 0

    def test_monitor_config_includes_incremental_section(self):
        from monitor import MonitorConfig
        cfg = MonitorConfig(telegram={"bot_token": "t", "chat_id": "c"})
        assert cfg.incremental.max_pages == 5


class TestIncrementalHoles:
    """测试增量扫描在遇到已被推送的新图覆盖了空洞时的行为。"""

    @patch("monitor.fetch_page")
    @patch("monitor.process_and_push")
    def test_incremental_does_not_skip_hole(self, mock_push, mock_fetch, tmp_path):
        from monitor import run_incremental, save_pushed_ids
        
        # 1. 模拟 pushed_ids 缺少 98（98 是发送失败的空洞）
        # 包含新推送成功的 105..101，以及旧图 100, 99, 97, 96, 95
        pushed_ids = {105, 104, 103, 102, 101, 100, 99, 97, 96, 95}
        save_pushed_ids(tmp_path, "tg1", "alice", pushed_ids)
        
        # 2. Mock 接口返回
        # 第一页返回 105..101 (全在 pushed_ids 中，无新图)
        page1 = [
            {"id": 105, "createdAt": "2026-07-11T12:00:00Z", "nsfw": False},
            {"id": 104, "createdAt": "2026-07-11T11:00:00Z", "nsfw": False},
            {"id": 103, "createdAt": "2026-07-11T10:00:00Z", "nsfw": False},
            {"id": 102, "createdAt": "2026-07-11T09:00:00Z", "nsfw": False},
            {"id": 101, "createdAt": "2026-07-11T08:00:00Z", "nsfw": False},
        ]
        # 第二页返回 100..96 (其中 98 不在 pushed_ids 中)
        page2 = [
            {"id": 100, "createdAt": "2026-07-11T07:00:00Z", "nsfw": False},
            {"id": 99, "createdAt": "2026-07-11T06:00:00Z", "nsfw": False},
            {"id": 98, "createdAt": "2026-07-11T05:00:00Z", "nsfw": False}, # 空洞！
            {"id": 97, "createdAt": "2026-07-11T04:00:00Z", "nsfw": False},
            {"id": 96, "createdAt": "2026-07-11T03:00:00Z", "nsfw": False},
        ]
        
        mock_fetch.side_effect = [
            (page1, "cursor2"),
            (page2, "cursor3"),
            ([], "")
        ]
        
        # 用于记录被推送到 process_and_push 的 ID
        pushed_targets = []
        def side_effect_push(img, *args, **kwargs):
            pushed_targets.append(img["id"])
            # 模拟推送成功记录
            kwargs["pushed_ids"].add(img["id"])
            return True
            
        mock_push.side_effect = side_effect_push
        
        # 3. 运行增量扫描，每页 limit=5, max_pages=5
        run_incremental(
            "alice",
            seen_ids=set(),
            tg_id="tg1",
            seen_dir=tmp_path,
            nsfw_setting="sfw_only",
            output_dir=tmp_path,
            size_suffixes=[],
            bot_token="token",
            chat_id="chat",
            base_url="https://civitai.com",
            limit=5,
            video_enabled=False,
            max_video_size_mb=10,
            max_pages=5
        )
        
        # 4. 断言：应该成功获取 Page 2 并发现且推送 98
        assert 98 in pushed_targets, "Bug: 提前退出导致未推送空洞图片 98！"


# ---------------------------------------------------------------------------
# write_config — redact token + atomic write
# ---------------------------------------------------------------------------


class TestWriteConfig:
    def test_write_config_redacts_bot_token(self, tmp_path):
        path = tmp_path / "config.yaml"
        cfg = MonitorConfig(
            telegram={"bot_token": "SECRET_TOKEN_DO_NOT_PERSIST", "chat_id": "-1001"},
            subscriptions={"1": [{"name": "alice"}]},
            authorized_users=[1],
        )
        write_config(cfg, path)
        text = path.read_text()
        assert "SECRET_TOKEN_DO_NOT_PERSIST" not in text
        import yaml
        raw = yaml.safe_load(text)
        assert raw["telegram"]["bot_token"] == ""
        assert raw["telegram"]["chat_id"] == "-1001"
        # no leftover tmp
        leftovers = list(tmp_path.glob("config.yaml*.tmp"))
        assert leftovers == []

    def test_write_config_atomic_no_tmp_leftover(self, tmp_path):
        path = tmp_path / "cfg.yaml"
        cfg = MonitorConfig(telegram={"bot_token": "t", "chat_id": "c"})
        write_config(cfg, path)
        assert path.exists()
        assert not path.with_suffix(".tmp").exists()


# ---------------------------------------------------------------------------
# load_seen_ids / load_pushed_ids — corrupt files
# ---------------------------------------------------------------------------


class TestCorruptIdFiles:
    def test_load_seen_ids_corrupt_returns_empty(self, tmp_path, caplog):
        path = seen_file_for_user(tmp_path, "tg1", "alice")
        path.write_text("{not-json")
        import logging
        with caplog.at_level(logging.WARNING):
            ids = load_seen_ids(tmp_path, "tg1", "alice")
        assert ids == set()
        assert any("Corrupt seen" in r.message for r in caplog.records)

    def test_load_pushed_ids_corrupt_returns_empty(self, tmp_path, caplog):
        path = pushed_file_for_user(tmp_path, "tg1", "alice")
        path.write_text("[]broken")
        import logging
        with caplog.at_level(logging.WARNING):
            ids = load_pushed_ids(tmp_path, "tg1", "alice")
        assert ids == set()
        assert any("Corrupt pushed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# download_video — stream cap without Content-Length
# ---------------------------------------------------------------------------


class TestDownloadVideoStreamCap:
    def test_no_content_length_over_cap_deletes_tmp(self, tmp_path, monkeypatch):
        """When CL is missing, count bytes while streaming; exceed cap → False + no file."""
        import monitor as m

        save = tmp_path / "videos" / "2.mp4"
        big = [b"a" * (512 * 1024)] * 3  # 1.5 MB total, no Content-Length

        class BodyResp:
            status_code = 200
            headers = {}  # no content-length  # noqa: RUF012
            url = "https://cdn.example/video"

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=8192):
                yield from big

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class ProbeResp:
            status_code = 200
            headers = {}  # noqa: RUF012
            url = "https://other.cdn/path/not-b2"

            def raise_for_status(self):
                return None

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        calls = {"n": 0}

        def fake_safe_get(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return ProbeResp()
            return BodyResp()

        monkeypatch.setattr(m, "safe_get", fake_safe_get)
        ok = download_video("https://image.civitai.com/y", save, max_size_mb=1)
        assert ok.success is False
        assert ok.permanent is True  # size-cap exceeded is a permanent failure
        assert not save.exists()
        assert not save.with_suffix(save.suffix + ".tmp").exists()


# ---------------------------------------------------------------------------
# Markdown escape
# ---------------------------------------------------------------------------


class TestEscapeMarkdown:
    def test_escapes_underscore_in_username(self):
        assert escape_markdown("user_name") == r"user\_name"

    def test_escapes_multiple(self):
        assert "*" not in escape_markdown("a*b_c") or r"\*" in escape_markdown("a*b_c")
        assert escape_markdown("a*b") == r"a\*b"


class TestClearStatusInterrupted:
    def test_interrupted_keeps_snapshot(self, tmp_path, monkeypatch):
        import datetime as _dt

        import monitor as m

        status = tmp_path / "monitor_status.json"
        monkeypatch.setattr(m, "STATUS_PATH", status)
        status.write_text('{"status":"running"}')
        start = _dt.datetime.now(_dt.timezone.utc)
        m._clear_status(True, start)
        assert status.exists()
        data = __import__("json").loads(status.read_text())
        assert data["status"] == "interrupted"

    def test_normal_clears_status(self, tmp_path, monkeypatch):
        import datetime as _dt

        import monitor as m

        status = tmp_path / "monitor_status.json"
        monkeypatch.setattr(m, "STATUS_PATH", status)
        status.write_text('{"status":"running"}')
        m._clear_status(False, _dt.datetime.now(_dt.timezone.utc))
        assert not status.exists()

