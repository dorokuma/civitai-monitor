"""Path-safety and isolation tests for monitor.py hardening.

Covers the defenses against tampered API responses:
  * item-id coercion and traversal-safe download paths (_coerce_item_id)
  * download-filename extension whitelist (IMAGE_EXT_WHITELIST / VIDEO_EXT_WHITELIST)
  * resolved-path containment (_is_path_within) as defense in depth
  * image download size cap (MAX_IMAGE_DOWNLOAD_MB, streaming abort)
  * createdAt Markdown escaping in captions
  * per-item and per-creator exception isolation
  * cleanup_old_caches lower bound for keep_days < 1
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import monitor as m
from monitor import (
    IMAGE_EXT_WHITELIST,
    MAX_IMAGE_DOWNLOAD_MB,
    VIDEO_EXT_WHITELIST,
    _coerce_item_id,
    _is_path_within,
    cleanup_old_caches,
    escape_markdown,
)

LOG = "civitai-monitor"


def _push_kwargs(output_dir: Path) -> dict:
    """Keyword args for process_and_push without any state persistence."""
    return dict(
        size_suffixes=[],
        output_dir=output_dir,
        bot_token="t",
        chat_id="c",
        video_enabled=False,
        max_video_size_mb=10,
    )


def _fetch_page_kwargs(output_dir: Path) -> dict:
    return dict(
        base_url="https://x",
        limit=10,
        size_suffixes=[],
        output_dir=output_dir,
        bot_token="t",
        chat_id="c",
        video_enabled=False,
        max_video_size_mb=10,
        pushed_dir=output_dir,
        tg_id="tg1",
    )


# ---------------------------------------------------------------------------
# t1 — malicious ids are rejected before any path is built
# ---------------------------------------------------------------------------


class TestCoerceItemId:
    @pytest.mark.parametrize(
        "raw",
        ["../../..", "/etc/passwd", "../x", "abc", "", None, [1], True, {}],
    )
    def test_malicious_ids_rejected(self, raw):
        assert _coerce_item_id(raw) is None

    @pytest.mark.parametrize(("raw", "want"), [(123, 123), ("456", 456)])
    def test_valid_ids_coerced_to_int(self, raw, want):
        assert _coerce_item_id(raw) == want
        assert isinstance(_coerce_item_id(raw), int)


class TestMaliciousIdDropped:
    def test_process_and_push_rejects_traversal_id(self, tmp_path, monkeypatch):
        """id="../../.." (and friends) never reaches download_image / disk."""
        calls = []

        def fake_download(url, save_path, **k):
            calls.append(save_path)
            save_path.write_bytes(b"x")
            return m.DownloadResult(True)

        monkeypatch.setattr(m, "download_image", fake_download)
        monkeypatch.setattr(m, "send_to_telegram", lambda *a, **k: True)

        for bad in ("../../..", "/etc/passwd", "12ab", None):
            item = {"id": bad, "url": "https://x.com/width=original/f.jpeg"}
            result = m.process_and_push(item, "alice", **_push_kwargs(tmp_path))
            assert result is False

        assert calls == []
        assert list(tmp_path.iterdir()) == []  # nothing written anywhere

    @patch("monitor.time.sleep")
    def test_fetch_page_drops_malformed_ids(self, _sleep, tmp_path, monkeypatch):
        """Malformed ids never enter page_ids nor the processing loop; the
        min(page_ids) TypeError in run_incremental is thereby impossible."""
        items = [
            {"id": 10, "url": "https://x.com/width=original/a.jpeg"},
            {"id": "../../..", "url": "https://x.com/width=original/b.jpeg"},
            {"id": "/abs/path", "url": "https://x.com/width=original/c.jpeg"},
            {"id": "999", "url": "https://x.com/width=original/d.jpeg"},
        ]
        monkeypatch.setattr(m, "fetch_page", lambda *a, **k: (items, ""))
        processed = []
        monkeypatch.setattr(
            m, "process_and_push",
            lambda img, *a, **k: processed.append(img["id"]) or True,
        )
        new_on_page, page_ids, _cursor = m._fetch_and_process_page(
            "alice", False, "", set(), set(), **_fetch_page_kwargs(tmp_path),
        )
        assert page_ids == {10, 999}           # "../../.." and "/abs/path" gone
        assert sorted(processed) == [10, 999]  # never handed to the push loop
        assert min(page_ids) == 10             # int-only: min() cannot TypeError

    @patch("monitor.time.sleep")
    def test_all_malformed_page_yields_empty_page_ids(self, _sleep, tmp_path, monkeypatch):
        """A fully corrupted page behaves like an empty page (no crash)."""
        items = [{"id": "../x", "url": "https://x.com/width=original/a.jpeg"}]
        monkeypatch.setattr(m, "fetch_page", lambda *a, **k: (items, ""))
        monkeypatch.setattr(
            m, "process_and_push",
            lambda img, *a, **k: pytest.fail("must not be called"),
        )
        new_on_page, page_ids, _cursor = m._fetch_and_process_page(
            "alice", False, "", set(), set(), **_fetch_page_kwargs(tmp_path),
        )
        assert new_on_page == []
        assert page_ids == set()


# ---------------------------------------------------------------------------
# t2 — non-whitelisted filename extensions are dropped
# ---------------------------------------------------------------------------


class TestExtWhitelist:
    @pytest.mark.parametrize(
        "bad_url",
        [
            "https://x.com/width=original/evil.php",
            "https://x.com/width=original/pic.svg",
            "https://x.com/width=original/pic.html",
            "https://x.com/width=original/pic.pth",
        ],
    )
    def test_non_whitelisted_ext_dropped(self, tmp_path, monkeypatch, bad_url):
        calls = []
        monkeypatch.setattr(
            m, "download_image",
            lambda *a, **k: calls.append(1) or m.DownloadResult(True),
        )
        item = {"id": 7, "url": bad_url}
        result = m.process_and_push(item, "alice", **_push_kwargs(tmp_path))
        assert result is False
        assert calls == []
        assert list(tmp_path.iterdir()) == []  # nothing written

    def test_whitelisted_ext_keeps_byte_identical_path(self, tmp_path, monkeypatch):
        seen = {}

        def fake_download(url, save_path, **k):
            seen["url"], seen["path"] = url, save_path
            return m.DownloadResult(True)

        monkeypatch.setattr(m, "download_image", fake_download)
        monkeypatch.setattr(m, "send_to_telegram", lambda *a, **k: True)
        item = {"id": 7, "url": "https://x.com/width=original/pic.jpeg"}
        assert m.process_and_push(item, "alice", **_push_kwargs(tmp_path)) is True
        assert seen["path"] == tmp_path / "7.jpeg"  # same path as before the fix
        assert seen["url"] == "https://x.com/width=original/pic.jpeg"

    def test_video_branch_uses_fixed_whitelisted_name(self, tmp_path, monkeypatch):
        seen = {}

        def fake_dv(url, save_path, max_size_mb):
            seen["path"] = save_path
            return m.DownloadResult(True)

        monkeypatch.setattr(m, "download_video", fake_dv)
        monkeypatch.setattr(m, "send_to_telegram", lambda *a, **k: True)
        item = {"id": 11, "type": "video", "url": "https://x.com/clip.mp4"}
        kwargs = _push_kwargs(tmp_path)
        kwargs["video_enabled"] = True
        assert m.process_and_push(item, "alice", **kwargs) is True
        assert seen["path"] == tmp_path / "videos" / "11.mp4"

    def test_mkv_url_routed_through_video_path(self, tmp_path, monkeypatch):
        """mkv/avi are in the video whitelist → handled by the video branch."""
        seen = {}

        def fake_dv(url, save_path, max_size_mb):
            seen["path"] = save_path
            return m.DownloadResult(True)

        monkeypatch.setattr(m, "download_video", fake_dv)
        monkeypatch.setattr(m, "send_to_telegram", lambda *a, **k: True)
        item = {"id": 12, "url": "https://x.com/clip.mkv"}
        kwargs = _push_kwargs(tmp_path)
        kwargs["video_enabled"] = True
        assert m.process_and_push(item, "alice", **kwargs) is True
        assert seen["path"] == tmp_path / "videos" / "12.mp4"

    def test_whitelist_contents(self):
        assert IMAGE_EXT_WHITELIST == {".jpeg", ".jpg", ".png", ".gif", ".webp"}
        assert VIDEO_EXT_WHITELIST == {".mp4", ".webm", ".mov", ".mkv", ".avi"}


# ---------------------------------------------------------------------------
# t3 — resolved-path containment (defense in depth)
# ---------------------------------------------------------------------------


class TestPathContainment:
    def test_is_path_within_basic(self, tmp_path):
        inside = tmp_path / "sub" / "f.jpeg"
        assert _is_path_within(inside, tmp_path) is True
        assert _is_path_within(tmp_path, tmp_path) is True
        outside = tmp_path.parent / "elsewhere.jpeg"
        assert _is_path_within(outside, tmp_path) is False
        assert _is_path_within(tmp_path / ".." / "up.jpeg", tmp_path) is False

    def test_symlink_escape_dropped_before_download(self, tmp_path, monkeypatch):
        """A filename that resolves outside output_dir (symlink) is refused."""
        out = tmp_path / "out"
        out.mkdir()
        link = out / "5.jpeg"
        link.symlink_to(tmp_path / "escape.jpeg")  # dangling, outside out/
        calls = []
        monkeypatch.setattr(
            m, "download_image",
            lambda *a, **k: calls.append(1) or m.DownloadResult(True),
        )
        item = {"id": 5, "url": "https://x.com/width=original/f.jpeg"}
        result = m.process_and_push(item, "alice", **_push_kwargs(out))
        assert result is False
        assert calls == []  # download never attempted


# ---------------------------------------------------------------------------
# t4 — image download size cap (streaming abort)
# ---------------------------------------------------------------------------


class TestImageSizeCap:
    def test_stream_over_cap_aborts_and_cleans_tmp(self, tmp_path, monkeypatch):
        """Streaming past the cap aborts, deletes tmp, permanent failure."""
        save = tmp_path / "1.jpeg"
        chunk = b"a" * (256 * 1024)

        class BodyResp:
            status_code = 200

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=65536):
                for _ in range(8):  # 2 MB total — over the 1 MB test cap
                    yield chunk

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(m, "safe_get", lambda *a, **k: BodyResp())
        monkeypatch.setattr(m, "MAX_IMAGE_DOWNLOAD_MB", 1)  # shrink cap for test
        result = m.download_image("https://x.com/f.jpeg", save)
        assert result.success is False
        assert result.permanent is True  # oversized cannot succeed on retry
        assert not save.exists()
        assert not save.with_suffix(save.suffix + ".tmp").exists()

    def test_under_cap_downloads_normally(self, tmp_path, monkeypatch):
        save = tmp_path / "2.jpeg"

        class BodyResp:
            status_code = 200

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=65536):
                yield b"hello world"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(m, "safe_get", lambda *a, **k: BodyResp())
        result = m.download_image("https://x.com/f.jpeg", save)
        assert result.success is True
        assert result.permanent is False
        assert save.read_bytes() == b"hello world"

    def test_default_cap_constant(self):
        assert MAX_IMAGE_DOWNLOAD_MB == 30


# ---------------------------------------------------------------------------
# t5 — createdAt is Markdown-escaped in captions
# ---------------------------------------------------------------------------


class TestCreatedAtEscape:
    def test_markdown_payload_in_created_at_is_escaped(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(m, "download_image", lambda *a, **k: m.DownloadResult(True))
        monkeypatch.setattr(
            m, "send_to_telegram", lambda *a, **k: sent.append(a[2]) or True,
        )
        payload = "2025-01-01T00:00:00Z_*_[x]`b`"
        item = {
            "id": 9,
            "url": "https://x.com/width=original/f.jpeg",
            "createdAt": payload,
        }
        assert m.process_and_push(item, "alice", **_push_kwargs(tmp_path)) is True
        caption = sent[0]
        assert payload not in caption                       # raw payload gone
        assert "🕐 " + escape_markdown(payload) in caption  # escaped form present

    def test_normal_timestamp_unchanged(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(m, "download_image", lambda *a, **k: m.DownloadResult(True))
        monkeypatch.setattr(
            m, "send_to_telegram", lambda *a, **k: sent.append(a[2]) or True,
        )
        item = {
            "id": 9,
            "url": "https://x.com/width=original/f.jpeg",
            "createdAt": "2025-01-01T00:00:00Z",
        }
        assert m.process_and_push(item, "alice", **_push_kwargs(tmp_path)) is True
        assert "🕐 2025-01-01T00:00:00Z" in sent[0]  # normal rendering intact

    def test_missing_created_at_renders_empty(self, tmp_path, monkeypatch):
        sent = []
        monkeypatch.setattr(m, "download_image", lambda *a, **k: m.DownloadResult(True))
        monkeypatch.setattr(
            m, "send_to_telegram", lambda *a, **k: sent.append(a[2]) or True,
        )
        item = {"id": 9, "url": "https://x.com/width=original/f.jpeg"}
        assert m.process_and_push(item, "alice", **_push_kwargs(tmp_path)) is True
        assert sent[0].endswith("🕐 ")


# ---------------------------------------------------------------------------
# t6 — per-item / per-creator exception isolation
# ---------------------------------------------------------------------------


class TestPerItemIsolation:
    @patch("monitor.time.sleep")
    def test_one_failing_item_does_not_stop_the_page(
        self, _sleep, tmp_path, monkeypatch, caplog,
    ):
        items = [
            {"id": i, "url": "https://x.com/width=original/f.jpeg"} for i in (1, 2, 3)
        ]
        monkeypatch.setattr(m, "fetch_page", lambda *a, **k: (items, ""))

        def fake_push(img, *a, **k):
            if img["id"] == 2:
                raise RuntimeError("boom")
            return True

        monkeypatch.setattr(m, "process_and_push", fake_push)
        with caplog.at_level(logging.ERROR, logger=LOG):
            new_on_page, page_ids, _cursor = m._fetch_and_process_page(
                "alice", False, "", set(), set(), **_fetch_page_kwargs(tmp_path),
            )
        assert page_ids == {1, 2, 3}
        assert new_on_page == items  # page result unaffected by the failure
        assert any("id=2" in r.getMessage() for r in caplog.records)

    @patch("monitor.time.sleep")
    def test_state_error_from_one_item_does_not_stop_the_page(
        self, _sleep, tmp_path, monkeypatch,
    ):
        from state_store import StateWriteError

        items = [
            {"id": i, "url": "https://x.com/width=original/f.jpeg"} for i in (1, 2)
        ]
        monkeypatch.setattr(m, "fetch_page", lambda *a, **k: (items, ""))

        def fake_push(img, *a, **k):
            if img["id"] == 1:
                raise StateWriteError("lock timeout")
            return True

        monkeypatch.setattr(m, "process_and_push", fake_push)
        new_on_page, page_ids, _cursor = m._fetch_and_process_page(
            "alice", False, "", set(), set(), **_fetch_page_kwargs(tmp_path),
        )
        assert page_ids == {1, 2}  # second item still processed


def _fake_cfg() -> SimpleNamespace:
    return SimpleNamespace(
        mode="incremental",
        nsfw="both",
        download=SimpleNamespace(size_suffixes=[]),
        telegram=SimpleNamespace(bot_token="t", chat_id="c"),
        api=SimpleNamespace(base_url="https://x", images_per_page=10),
        video_enabled=False,
        max_video_size_mb=10,
        incremental=SimpleNamespace(max_pages=5, hole_window_items=500),
        reconciliation=SimpleNamespace(
            max_pages_per_track=200, max_consecutive_no_new_pages=3,
        ),
    )


class TestPerCreatorIsolation:
    def _run_queue(self, monkeypatch, tmp_path, subs, fake_psc):
        monkeypatch.setattr(m, "STATUS_PATH", tmp_path / "status.json")
        monkeypatch.setattr(m, "_monitor_shutdown_requested", False)
        monkeypatch.setattr(m, "_process_single_creator", fake_psc)
        return m._process_creator_queue(
            subs, _fake_cfg(),
            seen_dir=tmp_path / "state", output_dir=tmp_path / "dl",
            user_filter="", start_time=_dt.datetime.now(_dt.timezone.utc),
        )

    def test_one_failing_creator_does_not_stop_the_round(
        self, tmp_path, monkeypatch, caplog,
    ):
        calls = []

        def fake_psc(username, tg_id_str, seen_dir, output_dir, **k):
            calls.append(username)
            if username == "bad":
                raise RuntimeError("boom")
            return set(), 1

        subs = {"tg1": [{"name": "good"}, {"name": "bad"}, {"name": "good2"}]}
        with caplog.at_level(logging.ERROR, logger=LOG):
            pushed, had_fetch_error = self._run_queue(monkeypatch, tmp_path, subs, fake_psc)
        assert calls == ["good", "bad", "good2"]  # loop continued past "bad"
        assert pushed == 2
        assert had_fetch_error is False
        assert any("@bad" in r.getMessage() for r in caplog.records)

    def test_fetch_page_error_is_contained_and_reported(self, tmp_path, monkeypatch):
        from monitor import FetchPageError

        calls = []

        def fake_psc(username, tg_id_str, seen_dir, output_dir, **k):
            calls.append(username)
            if username == "badnet":
                raise FetchPageError("api down")
            return set(), 0

        subs = {"tg1": [{"name": "badnet"}, {"name": "after"}]}
        pushed, had_fetch_error = self._run_queue(monkeypatch, tmp_path, subs, fake_psc)
        assert calls == ["badnet", "after"]  # next creator still scanned
        assert pushed == 0
        assert had_fetch_error is True       # main() will still exit 2

    def test_user_filter_and_entry_shapes_still_work(self, tmp_path, monkeypatch):
        calls = []

        def fake_psc(username, tg_id_str, seen_dir, output_dir, **k):
            calls.append(username)
            return set(), 0

        subs = {"tg1": ["plain_str", {"name": "named"}]}
        self._run_queue(monkeypatch, tmp_path, subs, fake_psc)
        assert calls == ["plain_str", "named"]


# ---------------------------------------------------------------------------
# cleanup_old_caches — keep_days < 1 lower bound
# ---------------------------------------------------------------------------


class TestCleanupLowerBound:
    @staticmethod
    def _touch_old(path: Path, days_ago: int) -> None:
        path.touch()
        mtime = time.time() - days_ago * 86400
        os.utime(path, (mtime, mtime))

    def test_keep_days_below_one_deletes_nothing(self, tmp_path, caplog):
        f = tmp_path / "old.txt"
        self._touch_old(f, days_ago=100)
        with caplog.at_level(logging.WARNING, logger=LOG):
            assert cleanup_old_caches(tmp_path, keep_days=0) == 0
            assert cleanup_old_caches(tmp_path, keep_days=-5) == 0
        assert f.exists()
        assert any("keep_days" in r.getMessage() for r in caplog.records)

    def test_keep_days_one_still_deletes_old_files(self, tmp_path):
        f = tmp_path / "old.txt"
        self._touch_old(f, days_ago=5)
        assert cleanup_old_caches(tmp_path, keep_days=1) == 1
        assert not f.exists()
