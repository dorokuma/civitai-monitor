"""Hardening round 2 for monitor.py.

Covers:
  * media upload failure must not mark the item pushed
    (send_to_telegram_detailed contract: delivered + media_failed)
  * message length budgets (_clip) against forged createdAt / long captions
  * meta field guard (non-dict meta must not raise AttributeError)
  * run_incremental shutdown_flag (aligned with run_full/run_reconciliation)
  * persistent process-lock file (_release_process_lock never unlinks)
  * atomic _write_status (tmp + os.replace, best-effort)
  * 0-byte download defense (permanent-style handling, no pointless retry)

The telegram_media.send_to_telegram_detailed contract consumed here:
    send_to_telegram_detailed(bot_token, chat_id, text, file_paths)
        -> tuple[delivered: bool, media_failed: bool]
It is always mocked in these tests (the real implementation lives in
telegram_media.py and is owned elsewhere).
"""

from __future__ import annotations

import datetime as _dt
import json
import os

import pytest

import monitor as m
from state_store import (
    load_pending_map,
    load_push_timestamps,
    mark_inflight,
    mark_pending,
)


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _run_push(
    tmp_path,
    monkeypatch,
    detailed_result,
    *,
    item_id=42,
    item=None,
    download="success",
):
    """Drive process_and_push with send_to_telegram_detailed mocked.

    ``download``: "success" (a 1-byte file lands on disk), "zero" (a 0-byte
    file lands), "permanent" (download reports a permanent failure).

    Returns (result, pushed_ids, captured) where captured holds the text and
    file_paths of the single send_to_telegram_detailed call.
    """
    captured: dict = {}

    def fake_detailed(bot_token, chat_id, text, file_paths):
        captured["text"] = text
        captured["files"] = file_paths
        return detailed_result

    monkeypatch.setattr(m, "send_to_telegram_detailed", fake_detailed)

    if download == "success":
        def fake_download(url, save_path, timeout=120):
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(b"x")
            return m.DownloadResult(True)
    elif download == "zero":
        def fake_download(url, save_path, timeout=120):
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.touch()
            return m.DownloadResult(True)
    elif download == "permanent":
        def fake_download(url, save_path, timeout=120):
            return m.DownloadResult(False, True)
    else:  # pragma: no cover - test authoring guard
        raise ValueError(download)

    monkeypatch.setattr(m, "download_image", fake_download)

    pushed: set[int] = set()
    if item is None:
        item = {"id": item_id, "url": "https://x.com/width=1024/f.jpeg"}
    result = m.process_and_push(
        item, "alice",
        size_suffixes=[], output_dir=tmp_path, bot_token="tok", chat_id="chat",
        video_enabled=False, max_video_size_mb=10,
        pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
    )
    return result, pushed, captured


# ---------------------------------------------------------------------------
# Media upload failure must not mark pushed (send_to_telegram_detailed)
# ---------------------------------------------------------------------------


class TestMediaUploadFailureNotPushed:
    def test_delivered_and_media_failed_parks_pending(self, tmp_path, monkeypatch):
        """(True, True): the text fallback reached the user but the locally
        downloaded media did not. The item must be parked as pending
        (retry capped by PENDING_MAX_RETRIES), never marked pushed, and the
        inflight guard cleared only after the pending state is persisted."""
        mark_inflight(tmp_path, "tg1", "alice", 42)
        result, pushed, captured = _run_push(tmp_path, monkeypatch, (True, True))
        assert result is False
        assert 42 not in pushed
        assert 42 in load_pending_map(tmp_path, "tg1", "alice")
        assert 42 not in load_push_timestamps(tmp_path, "inflight", "tg1", "alice")
        # The media upload was still attempted with the downloaded file.
        assert captured["files"] == [tmp_path / "42.jpeg"]

    def test_media_failed_preserves_pending_retries_counter(self, tmp_path, monkeypatch):
        """Parking after a media upload failure must not reset the retries
        counter, so PENDING_MAX_RETRIES keeps capping the retries."""
        mark_pending(tmp_path, "tg1", "alice", 42, ts=1.0, retries=1)
        mark_inflight(tmp_path, "tg1", "alice", 42)
        result, _pushed, _ = _run_push(tmp_path, monkeypatch, (True, True))
        assert result is False
        assert load_pending_map(tmp_path, "tg1", "alice")[42][1] == 1

    def test_delivered_without_media_failure_pushes_normally(self, tmp_path, monkeypatch):
        """(True, False): media went out with the text — pushed as before."""
        result, pushed, _ = _run_push(tmp_path, monkeypatch, (True, False))
        assert result is True
        assert pushed == {42}
        assert load_pending_map(tmp_path, "tg1", "alice") == {}
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}

    @pytest.mark.parametrize("media_failed", [False, True])
    def test_not_delivered_keeps_retry_semantics(self, tmp_path, monkeypatch, media_failed):
        """(False, *): the pre-existing not-delivered semantics hold — not
        pushed, definitive failure bookkeeping (inflight + pending cleared),
        item retried by the next scan because it never entered pushed_ids."""
        mark_inflight(tmp_path, "tg1", "alice", 42)
        result, pushed, _ = _run_push(tmp_path, monkeypatch, (False, media_failed))
        assert result is False
        assert 42 not in pushed
        assert 42 not in load_push_timestamps(tmp_path, "inflight", "tg1", "alice")
        assert 42 not in load_pending_map(tmp_path, "tg1", "alice")


# ---------------------------------------------------------------------------
# Message length budgets (_clip)
# ---------------------------------------------------------------------------


class TestClip:
    def test_short_text_unchanged(self):
        assert m._clip("hello", 100) == "hello"

    def test_truncation_appends_ellipsis_within_limit(self):
        clipped = m._clip("a" * 3000, 200)
        assert len(clipped) == 200
        assert clipped.endswith("…")
        assert len(m._clip("b" * 5000, m.TELEGRAM_TEXT_BUDGET)) == m.TELEGRAM_TEXT_BUDGET
        assert len(m._clip("c" * 5000, m.TELEGRAM_CAPTION_BUDGET)) == m.TELEGRAM_CAPTION_BUDGET

    def test_budgets_sit_below_telegram_hard_limits(self):
        assert m.TELEGRAM_CAPTION_BUDGET <= 1024
        assert m.TELEGRAM_TEXT_BUDGET <= 4096

    def test_clip_counts_utf16_units_not_codepoints(self):
        # Astral-plane characters cost 2 UTF-16 units each in the Bot API:
        # 600 mahjong tiles are 1200 units, well over the 1000 caption budget.
        clipped = m._clip("🀀" * 600, 1000)
        assert m._telegram_text_units(clipped) <= 1000
        assert clipped.endswith("…")

    def test_clip_drops_trailing_backslash_from_cut_escape(self):
        # Cutting "aa\aa\aa\..." at 10 units would leave a dangling Markdown
        # escape backslash; it must be dropped before the ellipsis.
        clipped = m._clip("aa\\" * 5, 10)
        assert clipped == "aa\\aa\\aa…"

    def test_clip_zero_limit_returns_empty(self):
        assert m._clip("anything", 0) == ""


class TestCreatedAtBudget:
    LONG = "x" * 3000

    def test_caption_clipped_for_media_message(self, tmp_path, monkeypatch):
        """A forged 3000-char createdAt must not push the media caption over
        the Telegram caption limit: it arrives clipped and marked."""
        item = {"id": 9, "url": "https://x.com/width=1024/f.jpeg", "createdAt": self.LONG}
        result, _pushed, captured = _run_push(
            tmp_path, monkeypatch, (True, False), item_id=9, item=item,
        )
        assert result is True
        assert captured["files"] is not None  # media (caption budget) path
        assert len(captured["text"]) <= m.TELEGRAM_CAPTION_BUDGET
        assert "…" in captured["text"]

    def test_plain_text_clipped_on_permanent_download_failure(self, tmp_path, monkeypatch):
        """The text-only permanent-failure notice is subject to the 4096
        message budget (clipped to 4000)."""
        item = {"id": 9, "url": "https://x.com/width=1024/f.jpeg", "createdAt": self.LONG}
        result, _pushed, captured = _run_push(
            tmp_path, monkeypatch, (True, False), item_id=9, item=item,
            download="permanent",
        )
        assert result is True
        assert captured["files"] is None  # plain-text path
        assert len(captured["text"]) <= m.TELEGRAM_TEXT_BUDGET
        assert "…" in captured["text"]


# ---------------------------------------------------------------------------
# meta field guard
# ---------------------------------------------------------------------------


class TestMetaFieldGuard:
    @pytest.mark.parametrize("bad_meta", ["evil", None, ["videoUrl"], 42])
    def test_non_dict_meta_does_not_raise(self, tmp_path, monkeypatch, bad_meta):
        """meta as str/None/list must be treated as "no videoUrl" — the old
        code raised AttributeError per item and retried forever."""
        download_calls: list[str] = []

        def fake_download(url, save_path, max_size_mb=1024):
            download_calls.append(url)
            return m.DownloadResult(True)

        monkeypatch.setattr(m, "download_video", fake_download)
        item = {"id": 7, "type": "video", "meta": bad_meta}
        result = m.process_and_push(
            item, "alice",
            size_suffixes=[], output_dir=tmp_path, bot_token="t", chat_id="c",
            video_enabled=True, max_video_size_mb=10,
            pushed_ids=set(), pushed_dir=tmp_path, tg_id="tg1",
        )
        assert result is False  # no videoUrl → dropped, no crash
        assert download_calls == []  # nothing was downloaded

    def test_dict_meta_still_supplies_videourl(self, tmp_path, monkeypatch):
        """A dict meta keeps working: videoUrl is picked up as before."""
        calls: list[tuple] = []

        def fake_download(url, save_path, max_size_mb=1024):
            calls.append((url, save_path))
            return m.DownloadResult(False, True)  # permanent → text-only send

        monkeypatch.setattr(m, "download_video", fake_download)
        monkeypatch.setattr(m, "send_to_telegram_detailed", lambda *a, **k: (True, False))
        item = {"id": 7, "type": "video", "meta": {"videoUrl": "https://cdn/v.mp4"}}
        result = m.process_and_push(
            item, "alice",
            size_suffixes=[], output_dir=tmp_path, bot_token="t", chat_id="c",
            video_enabled=True, max_video_size_mb=10,
            pushed_ids=set(), pushed_dir=tmp_path, tg_id="tg1",
        )
        assert result is True
        assert calls and calls[0][0] == "https://cdn/v.mp4"


# ---------------------------------------------------------------------------
# run_incremental shutdown_flag
# ---------------------------------------------------------------------------


class TestRunIncrementalShutdown:
    def test_shutdown_flag_stops_paging_early(self, tmp_path, monkeypatch):
        """Once shutdown_flag turns True, pagination stops at the top of the
        page loop (well before max_pages) and collected results are returned."""
        calls = {"fetch": 0}

        def fake_fetch(username, **kwargs):
            calls["fetch"] += 1
            # A fresh item on every page: the loop would never exhaust or
            # catch up on its own — only the shutdown flag can stop it.
            return [{"id": 1000 + calls["fetch"]}], f"cursor-{calls['fetch']}"

        monkeypatch.setattr(m, "fetch_page", fake_fetch)
        monkeypatch.setattr(m.time, "sleep", lambda _s: None)

        def fake_download(url, save_path, timeout=120):
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(b"x")
            return m.DownloadResult(True)

        monkeypatch.setattr(m, "download_image", fake_download)
        monkeypatch.setattr(m, "send_to_telegram_detailed", lambda *a, **k: (True, False))

        seen: set[int] = set()
        result = m.run_incremental(
            "alice",
            seen_ids=seen,
            tg_id="tg1",
            seen_dir=tmp_path,
            nsfw_setting="both",
            output_dir=tmp_path,
            size_suffixes=[],
            bot_token="t",
            chat_id="c",
            base_url="https://api.example",
            limit=10,
            video_enabled=False,
            max_video_size_mb=10,
            max_pages=50,
            shutdown_flag=lambda: calls["fetch"] >= 1,
        )
        # Page 1 fetched and processed, then the shutdown check stopped the
        # loop before page 2; the remaining tracks never fetched at all.
        assert calls["fetch"] == 1
        assert result == {1001}
        assert calls["fetch"] < 50

    def test_default_shutdown_flag_keeps_natural_exit(self, tmp_path, monkeypatch):
        """The default shutdown_flag (lambda: False) preserves the loop's
        normal exit conditions (empty page + no cursor → exhausted)."""

        def fake_fetch(username, **kwargs):
            return [], ""

        monkeypatch.setattr(m, "fetch_page", fake_fetch)
        monkeypatch.setattr(m.time, "sleep", lambda _s: None)
        result = m.run_incremental(
            "alice",
            seen_ids=set(),
            tg_id="tg1",
            seen_dir=tmp_path,
            nsfw_setting="sfw_only",
            output_dir=tmp_path,
            size_suffixes=[],
            bot_token="t",
            chat_id="c",
            base_url="https://api.example",
            limit=10,
            video_enabled=False,
            max_video_size_mb=10,
        )
        assert result == set()


# ---------------------------------------------------------------------------
# Process lock: persistent file (no unlink)
# ---------------------------------------------------------------------------


class TestProcessLockPersistentFile:
    def test_release_keeps_lock_file_and_flock_semantics(self, tmp_path, monkeypatch):
        """Releasing the lock closes the fd but deliberately keeps the lock
        file on disk (unlink-after-close race), and flock semantics stay
        intact: a second exclusive acquire is denied while held."""
        monkeypatch.setattr(m, "SCRIPT_DIR", tmp_path)
        fd = m._acquire_process_lock()
        assert fd is not None
        lock_file = tmp_path / m.LOCK_FILE_NAME
        assert lock_file.exists()
        # While held, a second exclusive acquire must be denied.
        assert m._acquire_process_lock() is None
        # Release closes the fd — and leaves the file in place.
        m._release_process_lock(fd, lock_file)
        assert lock_file.exists()
        with pytest.raises(OSError):
            os.fstat(fd)
        # The persistent file's lock is freely acquirable again.
        fd2 = m._acquire_process_lock()
        assert fd2 is not None
        m._release_process_lock(fd2, lock_file)


# ---------------------------------------------------------------------------
# Atomic status writes
# ---------------------------------------------------------------------------


class TestWriteStatusAtomic:
    def test_write_status_snapshot_no_tmp_left(self, tmp_path, monkeypatch):
        status_path = tmp_path / "monitor_status.json"
        monkeypatch.setattr(m, "STATUS_PATH", status_path)
        m._write_status(
            start_time=_utc_now(), mode="incremental", current_creator="alice",
            creators_done=1, creators_total=3, pushed_count=2,
        )
        data = json.loads(status_path.read_text())
        assert data["status"] == "running"
        assert data["mode"] == "incremental"
        assert data["current_creator"] == "alice"
        assert data["creators_done"] == 1
        assert data["pushed_count"] == 2
        assert not status_path.with_name(status_path.name + ".tmp").exists()

    def test_write_status_failure_is_logged_not_raised(self, tmp_path, monkeypatch):
        """Status is best-effort: a failed replace must not kill the scan."""
        status_path = tmp_path / "monitor_status.json"
        monkeypatch.setattr(m, "STATUS_PATH", status_path)

        def boom(*_a, **_k):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(m.os, "replace", boom)
        m._write_status(
            start_time=_utc_now(), mode="full", current_creator="bob",
            creators_done=0, creators_total=1, pushed_count=0,
        )
        assert not status_path.exists()
        assert not status_path.with_name(status_path.name + ".tmp").exists()


# ---------------------------------------------------------------------------
# 0-byte download defense
# ---------------------------------------------------------------------------


class TestZeroByteDownloadDefense:
    def test_zero_byte_file_sends_text_only_and_marks_pushed(self, tmp_path, monkeypatch):
        """A 0-byte download can never upload (Telegram 400 → text fallback
        with the media silently lost). It must be dropped: text-only send and
        permanent-style handling (pushed, no pointless retry)."""
        result, pushed, captured = _run_push(
            tmp_path, monkeypatch, (True, False), download="zero",
        )
        assert result is True
        assert 42 in pushed  # permanent-style handling
        assert captured["files"] is None  # the empty file was NOT uploaded


class TestDropEmptyFiles:
    def test_none_passthrough(self):
        assert m._drop_empty_files(None) is None

    def test_zero_byte_file_removed_good_file_kept(self, tmp_path):
        empty = tmp_path / "a.jpeg"
        empty.touch()
        good = tmp_path / "b.jpeg"
        good.write_bytes(b"x")
        assert m._drop_empty_files([empty, good]) == [good]

    def test_all_zero_byte_returns_none(self, tmp_path):
        empty = tmp_path / "a.jpeg"
        empty.touch()
        assert m._drop_empty_files([empty]) is None
