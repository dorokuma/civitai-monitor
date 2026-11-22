"""Strict At-Most-Once (零多推) regression tests.

Every scenario asserts the one hard guarantee: for a given item id, the
underlying Telegram POST count to a chat is NEVER more than 1 — across
retries, track switches (SFW → ALL), subscriber overlap, crashes and
upgrades. The 8 scenarios correspond to the joint reviewer+oracle spec:

  1. ReadTimeout after a possibly-delivered media → no re-POST ever.
  2. 502/504 / RemoteDisconnected → 1 POST, no retry, no text, claim kept.
  3. Media 400 → at most one text notice, claim kept, never re-sent.
  4. ConnectTimeout / NewConnectionError / 429(+Retry-After>60) → no text,
     safe rollback, retriable — and rollback must not clobber other ids.
  5. Corrupt ledger → StateReadError, ZERO posts, file untouched.
  6. Two tg_ids / duplicate id on the same page → exactly 1 POST to chat.
  7. Transient download failure → 0 posts, no state, retry pushes once.
  8. Upgrade: inflight/pending files absorbed into pushed, zero resend.
"""

from __future__ import annotations

import json

import pytest
import requests

import monitor as m
import telegram_media as tm
from state_store import (
    StateReadError,
    absorb_legacy_push_states,
    claim_push_slot,
    load_chat_pushed_ids,
    load_pushed_ids,
    mark_inflight,
    mark_pending,
    pushed_chat_file,
    pushed_file_for_user,
)

BOT_TOKEN = "123" "456:AA" "FakeTokenFor" "StrictAMO"
CHAT = "-100999"

ITEM_42 = {"id": 42, "url": "https://x.com/width=1024/f.jpeg",
           "createdAt": "2026-01-01T00:00:00Z"}


class _Api:
    """Counting stand-in for requests.post, routed per Telegram endpoint."""

    def __init__(self):
        self.posts = 0
        self.media_posts = 0
        self.text_posts = 0
        self.media_response = None      # MagicMock-like or None
        self.media_exc: list = []       # exceptions to raise, in order
        self.text_response = None

    def _resp(self, status, text="{}"):
        from unittest.mock import MagicMock

        r = MagicMock()
        r.status_code = status
        r.ok = 200 <= status < 300
        r.headers = {}
        r.text = text
        return r

    def __call__(self, url, timeout=None, **kwargs):
        self.posts += 1
        if "sendMessage" in url:
            self.text_posts += 1
            if self.text_response is not None:
                return self.text_response
            return self._resp(200)
        self.media_posts += 1
        if self.media_exc:
            exc = self.media_exc.pop(0)
            if exc is not None:
                raise exc
        if self.media_response is not None:
            return self.media_response
        return self._resp(200)


@pytest.fixture
def api(monkeypatch):
    stub = _Api()
    monkeypatch.setattr(tm.requests, "post", stub)
    monkeypatch.setattr(tm.time, "sleep", lambda *_a, **_k: None)
    return stub


def _scan(tmp_path, monkeypatch, pushed_ids, items, tg_id="tg1"):
    """One _fetch_and_process_page pass with downloads faked to 1-byte files.

    Uses the REAL telegram_media send path (requests.post is stubbed by the
    ``api`` fixture) so POST counts are exact.
    """
    def fake_download(url, save_path, timeout=120):
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(b"x")
        return m.DownloadResult(True)

    monkeypatch.setattr(m, "download_image", fake_download)
    monkeypatch.setattr(
        m, "fetch_page", lambda *a, **k: (list(items), "cursor-1"),
    )
    monkeypatch.setattr(m.time, "sleep", lambda *_: None)

    return m._fetch_and_process_page(
        "alice", False, "", set(), pushed_ids,
        base_url="https://civitai.example/api/v1/images",
        limit=100, sort="Newest", size_suffixes=[], output_dir=tmp_path,
        bot_token=BOT_TOKEN, chat_id=CHAT, video_enabled=False,
        max_video_size_mb=10, pushed_dir=tmp_path, tg_id=tg_id,
    )


# ---------------------------------------------------------------------------
# 1. ReadTimeout after a possibly-delivered send → never a 2nd POST
# ---------------------------------------------------------------------------


class TestReadTimeoutNoDuplicate:
    def test_media_group_readtimeout_single_post_across_scans(
        self, tmp_path, monkeypatch, api,
    ):
        api.media_exc = [requests.ReadTimeout("read timed out")]
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 1  # exactly one attempt
        # Uncertain → claim kept; id lands in pushed_ids.
        assert 42 in pushed
        assert 42 in load_chat_pushed_ids(tmp_path, CHAT)

        # Same scan again (e.g. SFW → ALL track pass, or next cron tick):
        # the id must never produce another POST.
        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 1

    def test_video_readtimeout_single_post(self, tmp_path, monkeypatch, api):
        api.media_exc = [requests.ReadTimeout("read timed out")]
        video = tmp_path / "v.mp4"
        video.write_bytes(b"VID")
        prepared = tm.prepare_media_for_upload([video])
        outcome = tm._send_telegram_video_outcome(
            "http://api/bot" + BOT_TOKEN, CHAT, "cap", prepared,
        )
        assert outcome is tm.SendOutcome.UNCERTAIN_NO_ROLLBACK
        assert api.posts == 1
        assert api.text_posts == 0


# ---------------------------------------------------------------------------
# 2. 5xx / post-connect disconnect → 1 POST, no fallback, claim kept
# ---------------------------------------------------------------------------


class TestUncertainNoRollback:
    @pytest.mark.parametrize("status", [502, 504])
    def test_media_5xx_single_post_claim_kept(
        self, tmp_path, monkeypatch, api, status,
    ):
        api.media_response = api._resp(status)
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 1      # no automatic 5xx retry
        assert api.text_posts == 0  # no text fallback
        assert 42 in pushed         # claim kept — never re-sent
        assert 42 in load_chat_pushed_ids(tmp_path, CHAT)

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 1

    def test_remote_disconnected_single_post(self, tmp_path, monkeypatch, api):
        api.media_exc = [
            requests.ConnectionError(
                "Connection aborted. RemoteDisconnected('Remote end "
                "closed connection without response')"
            )
        ]
        pushed: set[int] = set()
        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 1
        assert api.text_posts == 0
        assert 42 in pushed


# ---------------------------------------------------------------------------
# 3. Media 400 → at most one text notice, claim kept, never re-sent
# ---------------------------------------------------------------------------


class TestPermanentRejection:
    def test_400_text_notice_once_claim_kept(self, tmp_path, monkeypatch, api):
        api.media_response = api._resp(400, '{"ok":false}')
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.media_posts == 1
        assert api.text_posts == 1   # exactly one text notice
        assert api.posts == 2
        assert 42 in pushed

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 2  # nothing else ever goes out

    def test_400_text_notice_itself_fails_still_no_resend(
        self, tmp_path, monkeypatch, api,
    ):
        api.media_response = api._resp(400, '{"ok":false}')
        api.text_response = api._resp(500)
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 2
        assert 42 in pushed  # claim kept even though the notice failed


# ---------------------------------------------------------------------------
# 4. Connect failure / 429 → safe rollback, retriable, delta-write only
# ---------------------------------------------------------------------------


class TestDefinitelyNotSentRollback:
    def test_connect_timeout_rolls_back_and_retries_once(
        self, tmp_path, monkeypatch, api,
    ):
        api.media_exc = [requests.ConnectTimeout("connect timed out"), None]
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 1
        assert 42 not in pushed  # rolled back
        assert 42 not in load_chat_pushed_ids(tmp_path, CHAT)

        # Next scan: the retry goes through — total exactly 2 posts, one of
        # which provably never delivered anything.
        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 2
        assert pushed == {42}

    def test_new_connection_error_definitely_not_sent(self, tmp_path, api):
        try:
            from urllib3.exceptions import NewConnectionError
            exc = requests.ConnectionError("conn failed")
            exc.__cause__ = NewConnectionError(None, "refused")
        except ImportError:
            exc = requests.ConnectionError(
                "Failed to establish a new connection: refused"
            )
        api.media_exc = [exc]
        pushed: set[int] = set()

        def fake_download(url, save_path, timeout=120):
            save_path.write_bytes(b"x")
            return m.DownloadResult(True)

        monkey = pytest.MonkeyPatch()
        monkey.setattr(m, "download_image", fake_download)
        monkey.setattr(
            m, "fetch_page", lambda *a, **k: ([ITEM_42], ""),
        )
        monkey.setattr(m.time, "sleep", lambda *_: None)
        m._fetch_and_process_page(
            "alice", False, "", set(), pushed,
            base_url="https://x", limit=10, size_suffixes=[],
            output_dir=tmp_path, bot_token=BOT_TOKEN, chat_id=CHAT,
            video_enabled=False, max_video_size_mb=10,
            pushed_dir=tmp_path, tg_id="tg1",
        )
        monkey.undo()
        assert api.posts == 1
        assert 42 not in pushed
        assert 42 not in load_chat_pushed_ids(tmp_path, CHAT)

    def test_429_retry_after_over_cap_rolls_back(
        self, tmp_path, monkeypatch, api,
    ):
        resp = api._resp(429)
        resp.headers = {"Retry-After": "120"}
        api.media_response = resp
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 1
        assert api.text_posts == 0
        assert 42 not in pushed
        assert 42 not in load_chat_pushed_ids(tmp_path, CHAT)

    def test_rollback_preserves_concurrent_ids(self, tmp_path):
        """Delta-write rollback: discarding id 5 must not clobber other ids
        claimed after it."""
        assert claim_push_slot(tmp_path, CHAT, "tg1", "alice", 5)
        assert claim_push_slot(tmp_path, CHAT, "tg1", "alice", 6)
        assert claim_push_slot(tmp_path, CHAT, "tg2", "alice", 7)

        from state_store import rollback_push_slot
        rollback_push_slot(tmp_path, CHAT, "tg1", "alice", 5)

        assert load_chat_pushed_ids(tmp_path, CHAT) == {6, 7}
        assert load_pushed_ids(tmp_path, "tg1", "alice") == {6}
        assert load_pushed_ids(tmp_path, "tg2", "alice") == {7}


# ---------------------------------------------------------------------------
# 5. Corrupt ledger → StateReadError, ZERO posts, file untouched
# ---------------------------------------------------------------------------


class TestFailClosedLedger:
    def test_corrupt_user_ledger_zero_posts(self, tmp_path, monkeypatch, api):
        path = pushed_file_for_user(tmp_path, "tg1", "alice")
        path.write_text("{corrupt")
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 0
        assert path.read_text() == "{corrupt"  # never overwritten

    def test_corrupt_chat_ledger_zero_posts(self, tmp_path, monkeypatch, api):
        path = pushed_chat_file(tmp_path, CHAT)
        path.write_text("not json at all")
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 0
        assert path.read_text() == "not json at all"

    def test_load_pushed_ids_raises_state_read_error(self, tmp_path):
        path = pushed_file_for_user(tmp_path, "tg1", "alice")
        path.write_text('{"not": "a list"}')
        with pytest.raises(StateReadError):
            load_pushed_ids(tmp_path, "tg1", "alice")


# ---------------------------------------------------------------------------
# 6. Cross-subscriber / same-page dedup → exactly 1 POST to the chat
# ---------------------------------------------------------------------------


class TestChatLevelDedup:
    def test_two_tg_ids_same_creator_single_post(
        self, tmp_path, monkeypatch, api,
    ):
        pushed_a: set[int] = set()
        pushed_b: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed_a, [ITEM_42], tg_id="tg1")
        assert api.posts == 1
        assert pushed_a == {42}

        # A second subscriber scanning the same creator to the same chat.
        _scan(tmp_path, monkeypatch, pushed_b, [ITEM_42], tg_id="tg2")
        assert api.posts == 1  # refused by the chat ledger — zero new posts
        assert 42 in pushed_b

    def test_same_page_duplicate_id_single_post(
        self, tmp_path, monkeypatch, api,
    ):
        dupe = dict(ITEM_42)
        pushed: set[int] = set()
        _scan(tmp_path, monkeypatch, pushed, [ITEM_42, dupe])
        assert api.posts == 1

    def test_claim_push_slot_refuses_second_claim(self, tmp_path):
        assert claim_push_slot(tmp_path, CHAT, "tg1", "alice", 42) is True
        assert claim_push_slot(tmp_path, CHAT, "tg1", "alice", 42) is False
        assert claim_push_slot(tmp_path, CHAT, "tg2", "alice", 42) is False


# ---------------------------------------------------------------------------
# 7. Transient download failure → 0 posts, no state, retry pushes once
# ---------------------------------------------------------------------------


class TestTransientDownloadSilentRetry:
    def test_transient_download_failure_sends_nothing(
        self, tmp_path, monkeypatch, api,
    ):
        monkeypatch.setattr(
            m, "download_image",
            lambda *a, **k: m.DownloadResult(False, permanent=False),
        )
        monkeypatch.setattr(
            m, "fetch_page", lambda *a, **k: ([ITEM_42], ""),
        )
        monkeypatch.setattr(m.time, "sleep", lambda *_: None)
        pushed: set[int] = set()

        m._fetch_and_process_page(
            "alice", False, "", set(), pushed,
            base_url="https://x", limit=10, size_suffixes=[],
            output_dir=tmp_path, bot_token=BOT_TOKEN, chat_id=CHAT,
            video_enabled=False, max_video_size_mb=10,
            pushed_dir=tmp_path, tg_id="tg1",
        )
        assert api.posts == 0
        assert pushed == set()
        assert load_pushed_ids(tmp_path, "tg1", "alice") == set()

        # Next scan succeeds: exactly one post.
        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 1
        assert pushed == {42}


# ---------------------------------------------------------------------------
# 8. Upgrade: inflight/pending absorbed into pushed ledgers, zero resend
# ---------------------------------------------------------------------------


class TestUpgradeAbsorption:
    def test_inflight_and_pending_absorbed(self, tmp_path):
        mark_inflight(tmp_path, "tg1", "alice", 10)
        mark_pending(tmp_path, "tg1", "alice", 11, ts=1.0, retries=0,
                     media_failed=True)

        absorb_legacy_push_states(tmp_path, CHAT, "tg1", "alice")

        assert load_chat_pushed_ids(tmp_path, CHAT) == {10, 11}
        assert load_pushed_ids(tmp_path, "tg1", "alice") == {10, 11}
        assert not (tmp_path / "inflight_push_tg1_alice.json").exists()
        assert not (tmp_path / "pending_push_tg1_alice.json").exists()

    def test_absorbed_id_never_posted_on_scan(
        self, tmp_path, monkeypatch, api,
    ):
        mark_pending(tmp_path, "tg1", "alice", 42, ts=1.0, retries=0)
        pushed: set[int] = set()

        _scan(tmp_path, monkeypatch, pushed, [ITEM_42])
        assert api.posts == 0  # absorbed → never sent again
        assert 42 in pushed

    def test_first_use_chat_ledger_merges_history(self, tmp_path):
        """First-use migration unions every legacy pushed_ids_*.json into the
        chat ledger so an upgraded install never re-pushes its history."""
        pushed_file_for_user(tmp_path, "tg1", "alice")
        (tmp_path / "pushed_ids_tg1_alice.json").write_text(json.dumps([1, 2, 3]))
        (tmp_path / "pushed_ids_tg2_alice.json").write_text(json.dumps([3, 4]))

        merged = load_chat_pushed_ids(tmp_path, CHAT)
        assert merged == {1, 2, 3, 4}
        # Persisted for the next call.
        assert load_chat_pushed_ids(tmp_path, CHAT) == {1, 2, 3, 4}
