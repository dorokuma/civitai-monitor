"""Lifecycle tests for the pending push state and its media_failed (mf) flag.

Background: in v1.3.0 a media upload failure parked the item as pending, but
the very next scan (600s cadence < 1800s confirm window) silently promoted it
to pushed without re-sending — production logged only "promoting without
re-send" and zero real re-sends. The fix extends pending records from
(ts, retries) to (ts, retries, media_failed):

  mf=1 — the media is KNOWN not to have left this machine (upload failed
         after the text fallback, or a transient download failure): the next
         scan that finds the item on a page re-sends it unconditionally
         (PENDING_CONFIRM_SECONDS must not gate it); off-page it is kept
         pending instead of promoted.
  mf=0 — delivery merely uncertain (timeout, crash mid-send): the existing
         anti-duplicate semantics (fresh → promote without re-send) hold.

Legacy on-disk shapes (bare float, {"ts", "retries"}) still load with mf=0.
"""

from __future__ import annotations

import json
import logging
import time

import pytest

import monitor as m
from state_store import (
    PENDING_CONFIRM_SECONDS,
    PENDING_MAX_RETRIES,
    adopt_stale_inflight,
    load_pending_map,
    load_push_timestamps,
    mark_inflight,
    mark_pending,
)

# Fixture-only fake token, split across literals so no token-shaped string
# ever appears in this file.
BOT_TOKEN = "123" "456:AA" "FakeTokenFor" "Tests"

SCAN_CADENCE = 600.0  # production scan cadence; < PENDING_CONFIRM_SECONDS

ITEM_42 = {"id": 42, "url": "https://x.com/width=1024/f.jpeg"}
ITEM_999 = {"id": 999, "url": "https://x.com/width=1024/g.jpeg"}


def _scan(tmp_path, monkeypatch, pushed_ids, items, detailed_result):
    """Drive one _fetch_and_process_page scan with the send seam mocked.

    Records every send_to_telegram_detailed call as (text, file_paths) and
    returns the list. The download seam always succeeds with a 1-byte file.
    """
    calls: list[tuple[str, list]] = []

    def fake_detailed(bot_token, chat_id, text, file_paths):
        calls.append((text, [str(p) for p in (file_paths or [])]))
        return detailed_result

    def fake_download(url, save_path, timeout=120):
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(b"x")
        return m.DownloadResult(True)

    monkeypatch.setattr(m, "send_to_telegram_detailed", fake_detailed)
    monkeypatch.setattr(m, "download_image", fake_download)
    monkeypatch.setattr(
        m, "fetch_page", lambda *a, **k: (list(items), "cursor-1")
    )
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)

    m._fetch_and_process_page(
        "alice", False, "", set(), pushed_ids,
        base_url="https://civitai.example/api/trpc/image",
        limit=100, sort="Newest", size_suffixes=[], output_dir=tmp_path,
        bot_token=BOT_TOKEN, chat_id="chat", video_enabled=False,
        max_video_size_mb=10, pushed_dir=tmp_path, tg_id="tg1",
    )
    return calls


class TestMediaFailedResendLifecycle:
    """t1/t2: an mf=1 pending record must produce a real re-send, then
    terminate at the PENDING_MAX_RETRIES cap."""

    def test_t1_media_failed_pending_is_resent_next_scan_then_pushed(
        self, tmp_path, monkeypatch,
    ):
        # A media upload failed in the previous scan: (True, True) parked
        # the item as pending with mf=1. The ts is still "fresh" (600s old,
        # one scan cadence < PENDING_CONFIRM_SECONDS).
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=time.time() - SCAN_CADENCE, retries=0, media_failed=True,
        )
        pushed: set[int] = set()

        calls = _scan(tmp_path, monkeypatch, pushed, [ITEM_42], (True, False))

        # The pre-fix bug promoted the item without any send call; the fix
        # must actually re-send the downloaded media.
        assert len(calls) == 1
        assert calls[0][1] == [str(tmp_path / "42.jpeg")]
        assert pushed == {42}
        assert load_pending_map(tmp_path, "tg1", "alice") == {}
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}

    def test_t2_resent_failure_reaches_cap_then_terminal_promote_with_warning(
        self, tmp_path, monkeypatch, caplog,
    ):
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=time.time() - SCAN_CADENCE, retries=0, media_failed=True,
        )
        pushed: set[int] = set()

        # Scan 1: the re-send fails again (text fallback delivered, media not).
        calls = _scan(tmp_path, monkeypatch, pushed, [ITEM_42], (True, True))
        assert len(calls) == 1
        pending = load_pending_map(tmp_path, "tg1", "alice")
        assert pending[42][1] == PENDING_MAX_RETRIES  # retry consumed
        assert pending[42][2] == 1                    # still media_failed

        # Scan 2: retries at the cap → terminal promote with warning, and
        # NO further send attempt (fresh ts must not matter here either).
        with caplog.at_level(logging.WARNING, logger="civitai-monitor"):
            calls2 = _scan(tmp_path, monkeypatch, pushed, [ITEM_42], (True, True))
        assert calls2 == []
        assert 42 in pushed
        assert load_pending_map(tmp_path, "tg1", "alice") == {}
        terminal = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "id=42" in r.getMessage()
        ]
        assert terminal, "terminal mf=1 promote must log a warning"

    def test_t3_off_page_media_failed_pending_is_kept_not_promoted(
        self, tmp_path, monkeypatch, caplog,
    ):
        seeded_ts = time.time() - 2 * PENDING_CONFIRM_SECONDS
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=seeded_ts, retries=0, media_failed=True,
        )
        pushed: set[int] = set()

        # Item 42 is NOT on this page (only 999 is) — the exact cross-track
        # scenario: the SFW track scans first while the failed NSFW item is
        # off-page. Old behavior promoted it here (silent media loss).
        with caplog.at_level(logging.INFO, logger="civitai-monitor"):
            calls = _scan(
                tmp_path, monkeypatch, pushed, [ITEM_999], (True, False),
            )

        # 42 was neither promoted nor re-sent: the pending record survives
        # untouched even though its age exceeds PENDING_CONFIRM_SECONDS.
        pending = load_pending_map(tmp_path, "tg1", "alice")
        assert 42 not in pushed
        assert pending[42][0] == pytest.approx(seeded_ts, abs=5.0)
        assert pending[42][1] == 0
        assert pending[42][2] == 1
        assert all("42.jpeg" not in files for _text, files in calls)
        assert any(
            "off-page with media_failed=1" in r.getMessage()
            for r in caplog.records
        )


class TestUncertainSemanticsRegression:
    """t4: mf=0 (uncertain) keeps the pre-existing anti-duplicate semantics."""

    def test_t4_uncertain_fresh_on_page_promotes_without_resend(
        self, tmp_path, monkeypatch,
    ):
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=time.time() - SCAN_CADENCE, retries=0,
        )
        pushed: set[int] = set()

        calls = _scan(tmp_path, monkeypatch, pushed, [ITEM_42], (True, False))

        assert calls == []  # no re-send for a fresh uncertain record
        assert pushed == {42}
        assert load_pending_map(tmp_path, "tg1", "alice") == {}

    def test_t4b_uncertain_fresh_off_page_promotes_without_resend(
        self, tmp_path, monkeypatch,
    ):
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=time.time() - SCAN_CADENCE, retries=0,
        )
        pushed: set[int] = set()

        calls = _scan(tmp_path, monkeypatch, pushed, [ITEM_999], (True, False))

        assert pushed == {42, 999}
        assert load_pending_map(tmp_path, "tg1", "alice") == {}
        assert all("42.jpeg" not in files for _text, files in calls)

    def test_t4c_uncertain_expired_on_page_still_retries_once(
        self, tmp_path, monkeypatch,
    ):
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=time.time() - 2 * PENDING_CONFIRM_SECONDS, retries=0,
        )
        pushed: set[int] = set()

        calls = _scan(tmp_path, monkeypatch, pushed, [ITEM_42], (True, False))

        assert len(calls) == 1  # the historical "one retry push" still works
        assert pushed == {42}
        assert load_pending_map(tmp_path, "tg1", "alice") == {}


class TestAdoptStaleInflightMerge:
    """t5: adoption must keep the older ts and OR-merge the mf flag."""

    def test_t5_inflight_fresh_ts_does_not_override_pending_ts(self, tmp_path):
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=1000.0, retries=1, media_failed=True,
        )
        mark_inflight(tmp_path, "tg1", "alice", 42)  # re-stamped fresh by a retry
        mark_inflight(tmp_path, "tg1", "alice", 51)  # genuine crash mid-send

        pending = adopt_stale_inflight(tmp_path, "tg1", "alice")

        # The fresh inflight ts must NOT reset the confirm window mid-retry.
        assert pending[42][0] == 1000.0
        assert pending[42][1] == 1
        assert pending[42][2] == 1  # mf=1 survives adoption (mf | 0)
        # A new inflight id adopts as uncertain with the inflight ts.
        assert pending[51][1] == 0
        assert pending[51][2] == 0
        assert pending[51][0] > 1000.0
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}


class TestPendingStateBackwardCompat:
    """t6: on-disk shapes written by older versions must keep loading."""

    def test_t6_legacy_two_tuple_and_float_shapes_load_with_mf_zero(
        self, tmp_path,
    ):
        path = tmp_path / "pending_push_tg1_alice.json"
        path.write_text(json.dumps({
            "42": {"ts": 1000.0, "retries": 1},  # v1.3.0 dict shape (no mf)
            "43": 55.5,                           # ancient float-only shape
        }))

        data = load_pending_map(tmp_path, "tg1", "alice")

        assert data == {42: (1000.0, 1, 0), 43: (55.5, 0, 0)}

    def test_t6b_media_failed_roundtrips_through_disk(self, tmp_path):
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=1000.0, retries=1, media_failed=True,
        )

        on_disk = json.loads(
            (tmp_path / "pending_push_tg1_alice.json").read_text()
        )
        assert on_disk["42"] == {"ts": 1000.0, "retries": 1, "media_failed": 1}
        assert load_pending_map(tmp_path, "tg1", "alice") == {42: (1000.0, 1, 1)}


class TestTransientDownloadFailureDropPoint:
    """t7: the transient download-failure drop point parks mf=1."""

    def test_t7_transient_failure_parks_pending_with_media_failed(
        self, tmp_path, monkeypatch,
    ):
        # A pending entry with retries already consumed must keep its counter.
        mark_pending(tmp_path, "tg1", "alice", 42, ts=1.0, retries=1)
        monkeypatch.setattr(
            m, "download_image",
            lambda *a, **k: m.DownloadResult(False, permanent=False),
        )
        monkeypatch.setattr(m, "send_to_telegram", lambda *a, **k: True)
        monkeypatch.setattr(m.time, "sleep", lambda *_: None)
        pushed: set[int] = set()

        result = m.process_and_push(
            ITEM_42, "alice",
            size_suffixes=[], output_dir=tmp_path, bot_token=BOT_TOKEN,
            chat_id="chat", video_enabled=False, max_video_size_mb=10,
            pushed_ids=pushed, pushed_dir=tmp_path, tg_id="tg1",
        )

        assert result is False
        assert 42 not in pushed
        pending = load_pending_map(tmp_path, "tg1", "alice")
        assert pending[42][1] == 1   # retries counter reused, not reset
        assert pending[42][2] == 1   # media_failed=1 → real re-send next scan
        assert pending[42][0] > 1.0  # ts refreshed to now
