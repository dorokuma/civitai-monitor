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
    _monitor_signal_handler,
    adopt_stale_inflight,
    cleanup_old_caches,
    clear_pending,
    fetch_page,
    load_pending_map,
    load_push_timestamps,
    load_pushed_ids,
    load_seen_ids,
    mark_inflight,
    mark_pending,
    nsfw_tracks,
    normalize_to_original,
    pushed_file_for_user,
    save_pushed_ids,
    save_seen_ids,
    seen_file_for_user,
    update_pending_map,
    update_push_timestamps,
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
        with patch("monitor.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            with patch.dict(os.environ, {}, clear=False):
                fetch_page("alice", limit=10000)
        sent_params = mock_get.call_args.kwargs["params"]
        assert sent_params["limit"] == 200

    def test_clamps_zero_or_negative_to_one(self):
        with patch("monitor.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            fetch_page("alice", limit=0)
        assert mock_get.call_args.kwargs["params"]["limit"] == 1
        mock_get.reset_mock()
        with patch("monitor.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            fetch_page("alice", limit=-5)
        assert mock_get.call_args.kwargs["params"]["limit"] == 1

    def test_preserves_normal_limit(self):
        with patch("monitor.safe_get") as mock_get:
            mock_get.return_value = self._mock_response()
            fetch_page("alice", limit=100)
        assert mock_get.call_args.kwargs["params"]["limit"] == 100

    def test_uses_civitai_red_when_nsfw_true(self):
        """NSFW track must hit civitai.red."""
        with patch("monitor.safe_get") as mock_get:
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
        with patch("monitor.safe_get") as mock_get:
            mock_get.side_effect = [empty, items]
            fetched, cursor = fetch_page("alice", nsfw=False, sort="Newest")
        assert mock_get.call_count == 2
        # Second call must not have `sort` parameter
        second_params = mock_get.call_args_list[1].kwargs["params"]
        assert "sort" not in second_params
        assert len(fetched) == 1

    def test_returns_empty_and_no_cursor_on_empty_response(self):
        """When both attempts return 0 items, return ([], '') so the loop terminates."""
        with patch("monitor.safe_get") as mock_get:
            mock_get.return_value = self._mock_response(items=[])
            fetched, cursor = fetch_page("alice", nsfw=False, sort="Newest")
        assert fetched == []
        assert cursor == ""

    def test_network_error_returns_empty(self):
        """A 5xx must not crash the loop — return ([], '') and let the caller skip."""
        import requests as _req
        with patch("monitor.safe_get", side_effect=_req.RequestException("boom")):
            fetched, cursor = fetch_page("alice", nsfw=False, sort="Newest")
        assert fetched == []
        assert cursor == ""


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
        import monitor as m

        video = tmp_path / "1.mp4"
        video.write_bytes(b"fake-video")

        def boom(*_a, **_k):
            raise requests.Timeout("read timed out")

        text_calls: list = []
        monkeypatch.setattr(m.requests, "post", boom)
        monkeypatch.setattr(
            m, "_send_telegram_text",
            lambda *a, **k: text_calls.append(1) or True,
        )
        ok = m._send_telegram_video("http://api/botT", "chat", "caption", video)
        assert ok is None  # uncertain, not false success
        assert text_calls == []

    def test_media_group_timeout_returns_uncertain_no_text(self, monkeypatch, tmp_path):
        import monitor as m

        img = tmp_path / "1.jpeg"
        img.write_bytes(b"fake-img")

        def boom(*_a, **_k):
            raise requests.Timeout("read timed out")

        text_calls: list = []
        monkeypatch.setattr(m.requests, "post", boom)
        monkeypatch.setattr(
            m, "_send_telegram_text",
            lambda *a, **k: text_calls.append(1) or True,
        )
        ok = m._send_telegram_media_group("http://api/botT", "chat", "caption", [img])
        assert ok is None
        assert text_calls == []

    def test_video_http_error_still_falls_back_to_text(self, monkeypatch, tmp_path):
        import monitor as m

        video = tmp_path / "1.mp4"
        video.write_bytes(b"fake-video")

        class FakeResp:
            ok = False
            text = "bad request"

        monkeypatch.setattr(m.requests, "post", lambda *a, **k: FakeResp())
        text_calls: list = []
        monkeypatch.setattr(
            m, "_send_telegram_text",
            lambda *a, **k: text_calls.append(1) or True,
        )
        ok = m._send_telegram_video("http://api/botT", "chat", "caption", video)
        assert ok is True
        assert text_calls == [1]


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


