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

import pytest
import requests

import monitor as m
import state_store
import telegram_media as tm
from state_store import (
    StateReadError,
    StateWriteError,
    absorb_legacy_push_states,
    adopt_stale_inflight,
    claim_push_slot,
    load_chat_pushed_ids,
    load_pending_map,
    load_push_timestamps,
    load_pushed_ids,
    mark_inflight,
    mark_pending,
    pushed_chat_file,
    pushed_file_for_user,
    rollback_push_slot,
    save_pushed_ids,
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
        assert calls == []
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


# ---------------------------------------------------------------------------
# Global chat ledger wiring (merged from test_strict_at_most_once): first-use
# migration runs inside .pushed.lock from EVERY entry point; corrupt ledgers
# abort the whole page fail-closed; the global ledger filters new_on_page.
# ---------------------------------------------------------------------------

BOT_TOKEN_AMO = "123" "456:AA" "FakeTokenFor" "StrictAMO"
CHAT_AMO = "-100999"
ITEM_AMO = {"id": 42, "url": "https://x.com/width=1024/f.jpeg",
            "createdAt": "2026-01-01T00:00:00Z"}


def _scan_real_send(tmp_path, monkeypatch, pushed_ids, items, tg_id="tg1",
                    download_counter=None):
    """One _fetch_and_process_page pass through the REAL send path —
    telegram_media.requests.post is stubbed by the caller's api stub."""
    def fake_download(url, save_path, timeout=120):
        if download_counter is not None:
            download_counter["n"] += 1
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
        bot_token=BOT_TOKEN_AMO, chat_id=CHAT_AMO, video_enabled=False,
        max_video_size_mb=10, pushed_dir=tmp_path, tg_id=tg_id,
    )


class TestChatLedgerWiring:
    """_ensure_chat_ledger_locked inside .pushed.lock: whichever entry runs
    first migrates ALL pushed_ids_*.json history before the ledger exists."""

    def test_first_use_migration_via_load(self, tmp_path):
        """load_chat_pushed_ids unions every legacy pushed_ids_*.json into
        the chat ledger so an upgrade never re-pushes its history."""
        (tmp_path / "pushed_ids_tg1_alice.json").write_text(json.dumps([1, 2, 3]))
        (tmp_path / "pushed_ids_tg2_alice.json").write_text(json.dumps([3, 4]))

        merged = load_chat_pushed_ids(tmp_path, CHAT_AMO)
        assert merged == {1, 2, 3, 4}
        # Persisted for the next call.
        assert load_chat_pushed_ids(tmp_path, CHAT_AMO) == {1, 2, 3, 4}

    def test_first_use_migration_via_claim_entry(self, tmp_path):
        """claim_push_slot (a production entry that can run before any
        load_chat_pushed_ids call) migrates the SAME history — the item is
        refused, never re-sent."""
        (tmp_path / "pushed_ids_tg1_alice.json").write_text(json.dumps([1, 2, 42]))
        # tg2 never pushed anything; without migration 42 would claim + send.
        claimed = claim_push_slot(tmp_path, CHAT_AMO, "tg2", "alice", 42)
        assert claimed is False
        assert load_chat_pushed_ids(tmp_path, CHAT_AMO) == {1, 2, 42}

    def test_first_use_migration_includes_legacy_inflight_pending(self, tmp_path):
        """claim_push_slot BEFORE any absorb unions leftover inflight/pending
        IDs into the chat ledger — a cross-subscriber claim for an uncertain
        legacy ID is refused (zero resend) even though the legacy files
        remain on disk for absorb to clean up (idempotent)."""
        (tmp_path / "pushed_ids_tg1_alice.json").write_text(json.dumps([1, 2]))
        # tg1 crashed mid-upgrade: id 10 was inflight, 11 pending.
        (tmp_path / "inflight_push_tg1_alice.json").write_text(json.dumps({"10": 1.0}))
        (tmp_path / "pending_push_tg1_alice.json").write_text(json.dumps({"11": {"ts": 1.0, "retries": 0}}))
        # tg2 runs first: bare claim entry must see the FULL history.
        assert claim_push_slot(tmp_path, CHAT_AMO, "tg2", "alice", 1) is False
        assert claim_push_slot(tmp_path, CHAT_AMO, "tg2", "alice", 10) is False
        assert load_chat_pushed_ids(tmp_path, CHAT_AMO) == {1, 2, 10, 11}
        # Legacy files left for absorb; a later absorb stays idempotent.
        absorb_legacy_push_states(tmp_path, CHAT_AMO, "tg1", "alice")
        assert load_chat_pushed_ids(tmp_path, CHAT_AMO) == {1, 2, 10, 11}
        assert load_pushed_ids(tmp_path, "tg1", "alice") == {1, 2, 10, 11}
        assert not (tmp_path / "inflight_push_tg1_alice.json").exists()
        assert not (tmp_path / "pending_push_tg1_alice.json").exists()

    def test_corrupt_inflight_fails_migration_closed(self, tmp_path):
        """A corrupt inflight_push_*.json must abort the first-use migration
        fail-closed — never seal a chat ledger missing unknown legacy IDs."""
        (tmp_path / "inflight_push_tg1_alice.json").write_text("{corrupt")
        with pytest.raises(StateReadError):
            claim_push_slot(tmp_path, CHAT_AMO, "tg2", "alice", 42)
        assert not pushed_chat_file(tmp_path, CHAT_AMO).exists()
        # The corrupt original is never deleted.
        assert (tmp_path / "inflight_push_tg1_alice.json").read_text() == "{corrupt"

    def test_corrupt_history_fails_migration_closed(self, tmp_path):
        """A corrupt pushed_ids_*.json must NOT be silently skipped —
        a partial migration would permanently lose unknown pushed IDs."""
        (tmp_path / "pushed_ids_tg1_alice.json").write_text("{corrupt")
        with pytest.raises(StateReadError):
            load_chat_pushed_ids(tmp_path, CHAT_AMO)
        # The chat ledger was NOT created from partial history.
        assert not pushed_chat_file(tmp_path, CHAT_AMO).exists()

    def test_corrupt_chat_ledger_aborts_page_zero_posts(self, tmp_path, monkeypatch):
        """A corrupt pushed_chat_*.json aborts the whole page fail-closed —
        the StateReadError propagates out of _fetch_and_process_page."""
        import telegram_media as tm

        posts = {"n": 0}
        monkeypatch.setattr(
            tm.requests, "post", lambda *a, **k: posts.__setitem__("n", posts["n"] + 1),
        )
        monkeypatch.setattr(tm.time, "sleep", lambda *_a, **_k: None)
        path = pushed_chat_file(tmp_path, CHAT_AMO)
        path.write_text("not json at all")
        pushed: set[int] = set()

        with pytest.raises(StateReadError):
            _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert posts["n"] == 0
        assert path.read_text() == "not json at all"  # never overwritten

    def test_corrupt_user_ledger_aborts_page_zero_posts(self, tmp_path, monkeypatch):
        """A corrupt pushed_ids_*.json also aborts the page — the first-use
        migration inside .pushed.lock surfaces it before any item send."""
        import telegram_media as tm

        posts = {"n": 0}
        monkeypatch.setattr(
            tm.requests, "post", lambda *a, **k: posts.__setitem__("n", posts["n"] + 1),
        )
        monkeypatch.setattr(tm.time, "sleep", lambda *_a, **_k: None)
        path = pushed_file_for_user(tmp_path, "tg1", "alice")
        path.write_text("{corrupt")
        pushed: set[int] = set()

        with pytest.raises(StateReadError):
            _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert posts["n"] == 0
        assert path.read_text() == "{corrupt"

    def test_cross_subscriber_same_chat_filtered_by_global_ledger(
        self, tmp_path, monkeypatch,
    ):
        """tg2 scanning the same creator to the same chat must NOT re-download
        / re-send items tg1 already pushed — the global chat ledger filters
        new_on_page BEFORE process_and_push (0 extra posts)."""
        import telegram_media as tm

        posts = {"n": 0}

        def counting_post(url, timeout=None, **kwargs):
            posts["n"] += 1
            resp = type("R", (), {})()
            resp.status_code = 200
            resp.ok = True
            resp.headers = {}
            resp.text = "{}"
            return resp

        monkeypatch.setattr(tm.requests, "post", counting_post)
        monkeypatch.setattr(tm.time, "sleep", lambda *_a, **_k: None)

        downloads = {"n": 0}
        pushed_a: set[int] = set()
        _scan_real_send(
            tmp_path, monkeypatch, pushed_a, [ITEM_AMO], tg_id="tg1",
            download_counter=downloads,
        )
        assert posts["n"] == 1
        assert downloads["n"] == 1

        # Second subscriber: global ledger filters the item before download.
        pushed_b: set[int] = set()
        _scan_real_send(
            tmp_path, monkeypatch, pushed_b, [ITEM_AMO], tg_id="tg2",
            download_counter=downloads,
        )
        assert posts["n"] == 1       # refused — zero new posts
        assert downloads["n"] == 1   # and zero wasted re-downloads

    def test_absorbed_ids_synced_into_caller_pushed_set(
        self, tmp_path, monkeypatch,
    ):
        """absorb_legacy_push_states returns the absorbed IDs so
        _fetch_and_process_page merges them into pushed_ids — an absorbed
        item never reaches download/send."""
        import telegram_media as tm

        posts = {"n": 0}
        monkeypatch.setattr(
            tm.requests, "post", lambda *a, **k: posts.__setitem__("n", posts["n"] + 1),
        )
        monkeypatch.setattr(tm.time, "sleep", lambda *_a, **_k: None)
        mark_inflight(tmp_path, "tg1", "alice", 42)
        pushed: set[int] = set()

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert posts["n"] == 0   # absorbed → never sent again
        assert 42 in pushed      # caller's in-memory set got the absorbed id


class _Api:
    """Counting stand-in for telegram_media.requests.post, routed per
    Telegram endpoint so media vs text posts are accounted separately."""

    def __init__(self):
        self.posts = 0
        self.media_posts = 0
        self.text_posts = 0
        self.media_response = None
        self.media_exc: list = []
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
def amo_api(monkeypatch):
    """Stub the low-level Telegram POST seam so end-to-end scans through
    _fetch_and_process_page → process_and_push → claim_push_slot hit the
    real send path with exact POST counts."""
    stub = _Api()
    monkeypatch.setattr(tm.requests, "post", stub)
    monkeypatch.setattr(tm.time, "sleep", lambda *_a, **_k: None)
    return stub


class TestEndToEndAtMostOnce:
    """Production-path guards: _fetch_and_process_page → process_and_push →
    claim_push_slot → real telegram_media send (requests.post stubbed).
    For every uncertain/rejected outcome the item id must NEVER produce a
    second POST across scans; only a provable no-send may retry."""

    def test_readtimeout_keeps_claim_single_post_across_scans(
        self, tmp_path, monkeypatch, amo_api,
    ):
        """ReadTimeout (UNCERTAIN): 1 POST ever; the claim survives on disk
        and a second identical scan (SFW → ALL pass / next cron tick)
        issues no further POST."""
        amo_api.media_exc = [requests.ReadTimeout("read timed out")]
        pushed: set[int] = set()

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert amo_api.posts == 1
        assert 42 in pushed
        # Claim kept durably in both ledgers.
        assert 42 in load_chat_pushed_ids(tmp_path, CHAT_AMO)
        assert 42 in load_pushed_ids(tmp_path, "tg1", "alice")

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert amo_api.posts == 1  # never re-POSTed

    def test_5xx_keeps_claim_single_post_across_scans(
        self, tmp_path, monkeypatch, amo_api,
    ):
        """HTTP 5xx (UNCERTAIN): no retry, no text fallback, claim kept;
        next scan is silent."""
        amo_api.media_response = amo_api._resp(502)
        pushed: set[int] = set()

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert amo_api.posts == 1
        assert amo_api.text_posts == 0
        assert 42 in pushed
        assert 42 in load_chat_pushed_ids(tmp_path, CHAT_AMO)

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert amo_api.posts == 1

    def test_400_fallback_text_failure_still_keeps_claim(
        self, tmp_path, monkeypatch, amo_api,
    ):
        """Media 400 → REJECTED_PERMANENT degrades to at most one text
        notice; when that notice itself fails (5xx / timeout) the claim
        is STILL kept and the next scan sends nothing."""
        amo_api.media_response = amo_api._resp(400, '{"ok":false}')
        amo_api.text_response = amo_api._resp(500)
        pushed: set[int] = set()

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert amo_api.media_posts == 1
        assert amo_api.text_posts == 1
        assert amo_api.posts == 2
        assert 42 in pushed
        assert 42 in load_chat_pushed_ids(tmp_path, CHAT_AMO)

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert amo_api.posts == 2  # nothing else ever goes out

    def test_connect_timeout_releases_claim_retry_once_and_page_dedup(
        self, tmp_path, monkeypatch, amo_api,
    ):
        """ConnectTimeout (DEFINITELY_NOT_SENT): claim released, next scan
        retries and lands exactly once — 2 POSTs total. A duplicate id on
        the same page still produces a single POST."""
        amo_api.media_exc = [requests.ConnectTimeout("connect timed out"), None]
        pushed: set[int] = set()

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO, dict(ITEM_AMO)])
        assert amo_api.posts == 1  # same-page duplicate → one attempt only
        assert 42 not in pushed    # claim released
        assert 42 not in load_chat_pushed_ids(tmp_path, CHAT_AMO)
        assert 42 not in load_pushed_ids(tmp_path, "tg1", "alice")

        _scan_real_send(tmp_path, monkeypatch, pushed, [ITEM_AMO])
        assert amo_api.posts == 2  # retry succeeded, exactly once
        assert pushed == {42}
        assert 42 in load_chat_pushed_ids(tmp_path, CHAT_AMO)
        assert 42 in load_pushed_ids(tmp_path, "tg1", "alice")


class TestRollbackSymmetricRestore:
    """rollback_push_slot mirrors claim_push_slot: a failure on the
    SECOND ledger write restores the first ledger — the two never split."""

    def test_user_write_failure_restores_chat_ledger(
        self, tmp_path, monkeypatch,
    ):
        """If the chat ledger was already rewritten (id removed) and the
        user-ledger write then fails (ENOSPC/IO), the chat ledger is
        restored to its pre-rollback set — preserving OTHER ids claimed
        concurrently — and StateWriteError still propagates."""
        assert claim_push_slot(tmp_path, CHAT_AMO, "tg1", "alice", 5)
        assert claim_push_slot(tmp_path, CHAT_AMO, "tg1", "alice", 42)
        assert claim_push_slot(tmp_path, CHAT_AMO, "tg2", "alice", 7)

        user_path = pushed_file_for_user(tmp_path, "tg1", "alice")
        real_write = state_store._atomic_write

        def fail_user_ledger(path, data):
            if path == user_path:
                raise StateWriteError("simulated ENOSPC")
            return real_write(path, data)

        monkeypatch.setattr(state_store, "_atomic_write", fail_user_ledger)

        with pytest.raises(StateWriteError):
            rollback_push_slot(tmp_path, CHAT_AMO, "tg1", "alice", 42)

        # Symmetric restore: the chat ledger still holds 42 (plus the
        # concurrent claims 5 and 7 — a delta restore, not a clobber).
        assert load_chat_pushed_ids(tmp_path, CHAT_AMO) == {5, 7, 42}
        assert load_pushed_ids(tmp_path, "tg1", "alice") == {5, 42}
        assert load_pushed_ids(tmp_path, "tg2", "alice") == {7}


class TestHoleWindowNotPollutedByGlobalLedger:
    """The global chat ledger (all creators) must never feed the
    per-creator safe_lower_bound window — other creators' higher ids
    would fake 'caught up' and silently skip un-pushed works."""

    def test_incremental_scans_past_global_ids_to_fill_hole(
        self, tmp_path, monkeypatch, amo_api,
    ):
        """bob already pushed ids 9001–9005 (chat ledger + his own user
        ledger); alice pushed 103–106 but has a hole at 102 one page
        deeper. run_incremental must reach page 2 and push 102 — with
        chat_ledger pollution the window bound would be 9001 and the
        scan would stop after page 1."""
        save_pushed_ids(tmp_path, "tg1", "alice", {103, 104, 105, 106})
        save_pushed_ids(tmp_path, "tg1", "bob", {9001, 9002, 9003, 9004, 9005})

        alice_item = lambda i: {"id": i, "url": f"https://x.com/{i}.jpeg"}
        pages = iter([
            ([alice_item(106), alice_item(105)], "cursor-1"),
            ([alice_item(104), alice_item(103), alice_item(102)], ""),
        ])

        monkeypatch.setattr(
            m, "fetch_page", lambda *a, **k: next(pages),
        )

        def fake_download(url, save_path, timeout=120):
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(b"x")
            return m.DownloadResult(True)

        monkeypatch.setattr(m, "download_image", fake_download)
        monkeypatch.setattr(m.time, "sleep", lambda *_: None)

        m.run_incremental(
            "alice",
            seen_ids=set(),
            tg_id="tg1",
            seen_dir=tmp_path,
            nsfw_setting="sfw_only",
            output_dir=tmp_path,
            size_suffixes=[],
            bot_token=BOT_TOKEN_AMO,
            chat_id=CHAT_AMO,
            base_url="https://civitai.example/api/v1/images",
            limit=2,
            video_enabled=False,
            max_video_size_mb=10,
            max_pages=10,
            hole_window_items=5,
        )

        # The hole on page 2 was reached and pushed exactly once — bob's
        # 500-id window never leaked into alice's bound.
        assert amo_api.posts == 1
        assert amo_api.media_posts == 1
        assert 102 in load_chat_pushed_ids(tmp_path, CHAT_AMO)
        assert 102 in load_pushed_ids(tmp_path, "tg1", "alice")
        # bob's ledger untouched; no cross-creator clobber.
        assert load_pushed_ids(tmp_path, "tg1", "bob") == {
            9001, 9002, 9003, 9004, 9005,
        }


class TestCrossSubscriberConvergesFirstPage:
    """N3 regression: a second subscriber (tg2) whose own (tg_id, user)
    ledger is empty must still converge on the FIRST page — the on-page
    intersection page_ids & chat_pushed feeds safe_lower_bound, so
    run_incremental breaks instead of burning max_pages every scan."""

    def test_tg2_empty_ledger_converges_after_one_page(
        self, tmp_path, monkeypatch, amo_api,
    ):
        """tg1 already pushed alice's ids 1..10 (user ledger, migrated
        into the chat ledger on first load). tg2 has NO
        pushed_ids_tg2_alice.json. Pre-fix every page computed
        safe_lower_bound == 0, so the scan paged to max_pages with zero
        posts; post-fix page 1's {9, 10} ∩ chat_pushed lands in
        pushed_ids → bound 9 ≥ page_min_id → 'caught up after 1 pages'."""
        save_pushed_ids(tmp_path, "tg1", "alice", set(range(1, 11)))

        alice_item = lambda i: {"id": i, "url": f"https://x.com/{i}.jpeg"}
        page_data = [
            ([alice_item(10), alice_item(9)], "c2"),
            ([alice_item(8), alice_item(7)], "c3"),
            ([alice_item(6), alice_item(5)], "c4"),
            ([alice_item(4), alice_item(3)], "c5"),
            ([alice_item(2), alice_item(1)], "c6"),
        ]
        pages_fetched = {"n": 0}

        def fake_fetch(*a, **k):
            pages_fetched["n"] += 1
            return page_data[pages_fetched["n"] - 1]

        monkeypatch.setattr(m, "fetch_page", fake_fetch)
        downloads = {"n": 0}

        def fake_download(url, save_path, timeout=120):
            downloads["n"] += 1
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(b"x")
            return m.DownloadResult(True)

        monkeypatch.setattr(m, "download_image", fake_download)
        monkeypatch.setattr(m.time, "sleep", lambda *_: None)

        m.run_incremental(
            "alice",
            seen_ids=set(),
            tg_id="tg2",
            seen_dir=tmp_path,
            nsfw_setting="sfw_only",
            output_dir=tmp_path,
            size_suffixes=[],
            bot_token=BOT_TOKEN_AMO,
            chat_id=CHAT_AMO,
            base_url="https://civitai.example/api/v1/images",
            limit=2,
            video_enabled=False,
            max_video_size_mb=10,
            max_pages=5,
            hole_window_items=5,
        )

        # Converged on page 1 — did NOT burn all max_pages; zero posts
        # and zero downloads (every id already in the chat ledger).
        assert pages_fetched["n"] == 1
        assert amo_api.posts == 0
        assert downloads["n"] == 0
        # tg2's own ledger stays empty: the fix only feeds the in-memory
        # bound, it never claims ownership of tg1's pushes.
        assert load_pushed_ids(tmp_path, "tg2", "alice") == set()
