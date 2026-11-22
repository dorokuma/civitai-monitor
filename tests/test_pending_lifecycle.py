"""Strict at-most-once lifecycle: legacy inflight/pending states are absorbed
into the pushed ledgers with ZERO re-sends (upgrade path), and the send
pipeline is claim → send-once → outcome-classified bookkeeping.

Under the old design a pending record could trigger a re-send on the next
scan — which is exactly how a delivered-but-unconfirmed item got pushed
twice. The strict contract instead treats every leftover lifecycle record
as "possibly already delivered": absorb → never send again.
"""

from __future__ import annotations

import json
import time

import monitor as m
from state_store import (
    PENDING_MAX_RETRIES,
    absorb_legacy_push_states,
    adopt_stale_inflight,
    claim_push_slot,
    load_chat_pushed_ids,
    load_pending_map,
    load_push_timestamps,
    load_pushed_ids,
    mark_inflight,
    mark_pending,
)

# Fixture-only fake token, split across literals so no token-shaped string
# ever appears in this file.
BOT_TOKEN = "123" "456:AA" "FakeTokenFor" "Tests"

ITEM_42 = {"id": 42, "url": "https://x.com/width=1024/f.jpeg"}
ITEM_999 = {"id": 999, "url": "https://x.com/width=1024/g.jpeg"}


def _scan(tmp_path, monkeypatch, pushed_ids, items):
    """Drive one _fetch_and_process_page scan with the send seam mocked.

    Records every send_media_outcome call and returns the list; the mock
    always answers DELIVERED_OK. The download seam always succeeds with a
    1-byte file.
    """
    calls: list[tuple[str, list]] = []

    def fake_outcome(bot_token, chat_id, text, file_paths, _compress=None, **kw):
        calls.append((text, [str(p) for p in (file_paths or [])]))
        return m.SendOutcome.DELIVERED_OK

    def fake_download(url, save_path, timeout=120):
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(b"x")
        return m.DownloadResult(True)

    monkeypatch.setattr(m, "send_media_outcome", fake_outcome)
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


class TestLegacyStateAbsorption:
    """Leftover inflight/pending files from the pre-strict design are folded
    into the pushed ledgers on the next scan — never re-sent."""

    def test_pending_id_is_absorbed_not_resent(self, tmp_path, monkeypatch):
        mark_pending(tmp_path, "tg1", "alice", 42, ts=time.time() - 600,
                     retries=0, media_failed=True)
        pushed: set[int] = set()

        calls = _scan(tmp_path, monkeypatch, pushed, [ITEM_42])

        # Zero sends for the legacy record — possibly already delivered.
        assert all("42.jpeg" not in files for _t, files in calls)
        assert 42 in pushed
        # Both ledgers hold the absorbed id; the legacy file is gone.
        assert 42 in load_chat_pushed_ids(tmp_path, "chat")
        assert 42 in load_pushed_ids(tmp_path, "tg1", "alice")
        assert load_pending_map(tmp_path, "tg1", "alice") == {}
        assert not (tmp_path / "pending_push_tg1_alice.json").exists()

    def test_inflight_id_is_absorbed_not_resent(self, tmp_path, monkeypatch):
        mark_inflight(tmp_path, "tg1", "alice", 7)
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42, ITEM_999])

        assert 7 in load_chat_pushed_ids(tmp_path, "chat")
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}
        assert not (tmp_path / "inflight_push_tg1_alice.json").exists()

    def test_absorb_legacy_push_states_direct(self, tmp_path):
        mark_inflight(tmp_path, "tg1", "alice", 10)
        mark_pending(tmp_path, "tg1", "alice", 11, ts=1.0, retries=0)

        absorb_legacy_push_states(tmp_path, "chat", "tg1", "alice")

        assert load_chat_pushed_ids(tmp_path, "chat") == {10, 11}
        assert load_pushed_ids(tmp_path, "tg1", "alice") == {10, 11}
        assert not (tmp_path / "inflight_push_tg1_alice.json").exists()
        assert not (tmp_path / "pending_push_tg1_alice.json").exists()

    def test_absorb_merges_with_existing_ledgers(self, tmp_path):
        """Absorption must UNION, never clobber IDs already in the ledgers."""
        assert claim_push_slot(tmp_path, "chat", "tg1", "alice", 99)
        mark_pending(tmp_path, "tg1", "alice", 11, ts=1.0, retries=0)

        absorb_legacy_push_states(tmp_path, "chat", "tg1", "alice")

        assert load_chat_pushed_ids(tmp_path, "chat") == {99, 11}
        assert load_pushed_ids(tmp_path, "tg1", "alice") == {99, 11}


class TestAdoptStaleInflightMerge:
    """Legacy adopt_stale_inflight API (kept for backward compat)."""

    def test_inflight_fresh_ts_does_not_override_pending_ts(self, tmp_path):
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=1000.0, retries=1, media_failed=True,
        )
        mark_inflight(tmp_path, "tg1", "alice", 42)
        mark_inflight(tmp_path, "tg1", "alice", 51)

        pending = adopt_stale_inflight(tmp_path, "tg1", "alice")

        assert pending[42][0] == 1000.0
        assert pending[42][1] == 1
        assert pending[42][2] == 1
        assert pending[51][1] == 0
        assert pending[51][2] == 0
        assert load_push_timestamps(tmp_path, "inflight", "tg1", "alice") == {}


class TestPendingStateBackwardCompat:
    """On-disk shapes written by older versions must keep loading."""

    def test_legacy_two_tuple_and_float_shapes_load_with_mf_zero(self, tmp_path):
        path = tmp_path / "pending_push_tg1_alice.json"
        path.write_text(json.dumps({
            "42": {"ts": 1000.0, "retries": 1},
            "43": 55.5,
        }))

        data = load_pending_map(tmp_path, "tg1", "alice")

        assert data == {42: (1000.0, 1, 0), 43: (55.5, 0, 0)}

    def test_media_failed_roundtrips_through_disk(self, tmp_path):
        mark_pending(
            tmp_path, "tg1", "alice", 42,
            ts=1000.0, retries=1, media_failed=True,
        )

        on_disk = json.loads(
            (tmp_path / "pending_push_tg1_alice.json").read_text()
        )
        assert on_disk["42"] == {"ts": 1000.0, "retries": 1, "media_failed": 1}
        assert load_pending_map(tmp_path, "tg1", "alice") == {42: (1000.0, 1, 1)}


class TestTransientDownloadFailureStrict:
    """Transient download failures now send NOTHING to Telegram and write
    nothing to the pushed ledgers — the item is retried silently next scan."""

    def test_transient_failure_no_telegram_no_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            m, "download_image",
            lambda *a, **k: m.DownloadResult(False, permanent=False),
        )
        sent: list = []
        monkeypatch.setattr(
            m, "send_to_telegram", lambda *a, **k: sent.append(1) or True,
        )
        monkeypatch.setattr(
            m, "send_media_outcome",
            lambda *a, **k: sent.append(1) or m.SendOutcome.DELIVERED_OK,
        )
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
        assert sent == []
        assert load_chat_pushed_ids(tmp_path, "chat") == set()
        assert load_pushed_ids(tmp_path, "tg1", "alice") == set()
        assert load_pending_map(tmp_path, "tg1", "alice") == {}

    def test_transient_failure_then_next_scan_pushes_once(
        self, tmp_path, monkeypatch,
    ):
        """After a transient failure, the next successful download+send
        produces exactly ONE post."""
        state = {"fail": True}
        sent: list = []

        def fake_download(url, save_path, timeout=120):
            if state["fail"]:
                return m.DownloadResult(False, permanent=False)
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(b"x")
            return m.DownloadResult(True)

        def fake_outcome(bot_token, chat_id, text, file_paths, _compress=None, **kw):
            sent.append(text)
            return m.SendOutcome.DELIVERED_OK

        monkeypatch.setattr(m, "download_image", fake_download)
        monkeypatch.setattr(m, "send_media_outcome", fake_outcome)
        monkeypatch.setattr(m.time, "sleep", lambda *_: None)
        pushed: set[int] = set()

        kwargs = {
            "size_suffixes": [], "output_dir": tmp_path, "bot_token": BOT_TOKEN,
            "chat_id": "chat", "video_enabled": False, "max_video_size_mb": 10,
            "pushed_ids": pushed, "pushed_dir": tmp_path, "tg_id": "tg1",
        }
        assert m.process_and_push(ITEM_42, "alice", **kwargs) is False
        assert sent == []

        state["fail"] = False
        assert m.process_and_push(ITEM_42, "alice", **kwargs) is True
        assert len(sent) == 1  # exactly one post across both attempts
        assert pushed == {42}


def test_pending_max_retries_is_zero():
    """Strict contract: the pending auto-resend machinery is off."""
    assert PENDING_MAX_RETRIES == 0
