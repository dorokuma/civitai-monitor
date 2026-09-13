"""Tests for the weekly Telegram report (Sunday digest cron and helpers).

Covers:
  * _build_weekly_report — pure message assembly (normal / empty / ⚠️ paths).
  * The scan-outcome counters that feed the report (success / failure / 75)
    and that they are reset after the report is sent.
  * The Sunday-21:00-UTC trigger decision (fires on Sunday, not otherwise).
  * The durable push-history data source in state_store (roundtrip, prune,
    corrupt file) and the per-subscription collector over the 7-day window.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Load civitai-bot.py as a module (hyphenated name needs importlib) — same
# approach as tests/test_bot.py, but a separate module object so patches here
# never leak into the other test file.
_spec = importlib.util.spec_from_file_location(
    "civitai_bot_weekly_module", str(Path(__file__).parent.parent / "civitai-bot.py")
)
civitai_bot = importlib.util.module_from_spec(_spec)
sys.modules["civitai_bot_weekly_module"] = civitai_bot
_spec.loader.exec_module(civitai_bot)

from civitai_bot_weekly_module import (
    _build_weekly_report,
    _collect_weekly_creator_stats,
    _weekly_report_due,
    scheduled_scan_cron,
    scheduled_weekly_report_cron,
)

import monitor as monitor_mod
from state_store import (
    StateWriteError,
    load_push_history,
    record_push_history,
)

# 2026-09-13 is a Sunday; 21:05 UTC is past the 21:00 trigger time.
FIXED_SUNDAY = datetime(2026, 9, 13, 21, 5, tzinfo=timezone.utc)


class _FixedDatetime(datetime):
    """datetime replacement whose now() always returns FIXED_SUNDAY."""

    @classmethod
    def now(cls, tz=None):
        return FIXED_SUNDAY


@pytest.fixture(autouse=True)
def _reset_weekly_state():
    """Isolate the module-level weekly-report state between tests."""
    civitai_bot._reset_scan_stats()
    civitai_bot._weekly_report_last_sent_date = None
    yield
    civitai_bot._reset_scan_stats()
    civitai_bot._weekly_report_last_sent_date = None
    civitai_bot._alert_bot = None
    civitai_bot._ADMIN_CHAT_IDS = []
    civitai_bot._shutdown_requested = False


# ---------------------------------------------------------------------------
# _build_weekly_report — pure message assembly
# ---------------------------------------------------------------------------


class TestBuildWeeklyReport:
    def test_normal_report_lists_counts_and_creators(self):
        now = FIXED_SUNDAY
        stats = {"success": 960, "failure": 2, "skipped": 10}
        creators = [
            {
                "username": "alice",
                "pushes_7d": 12,
                "last_push_ts": now.timestamp() - 86400,
            },
            {
                "username": "bob",
                "pushes_7d": 3,
                "last_push_ts": now.timestamp() - 3 * 86400,
            },
        ]
        text = _build_weekly_report(stats, creators, "2026-09-12", now=now)
        assert "✅ 成功: 960 次" in text
        assert "❌ 失败: 2 次" in text
        assert "⏭️ 跳过: 10 次" in text
        assert "@alice: 近 7 天 12 条" in text
        assert "最近推送 2026-09-12" in text  # now - 1 day
        assert "最近成功: 2026-09-12" in text
        # Nobody is stale in this fixture: no warning marker at all.
        assert "⚠️" not in text

    def test_empty_report_handles_no_data(self):
        now = FIXED_SUNDAY
        empty = {"success": 0, "failure": 0, "skipped": 0}
        text = _build_weekly_report(empty, [], None, now=now)
        assert "（当前无订阅）" in text
        assert "✅ 成功: 0 次" in text
        assert "最近成功: 无记录 ⚠️" in text

    def test_missing_keys_default_to_zero(self):
        now = FIXED_SUNDAY
        text = _build_weekly_report({}, [{"username": "a", "pushes_7d": 0,
                                          "last_push_ts": None}], None, now=now)
        assert "✅ 成功: 0 次" in text
        assert "@a: 近 7 天 0 条" in text
        assert "最近推送 无记录" in text

    def test_stale_subscription_gets_warning_marker(self):
        now = FIXED_SUNDAY
        stale_ts = now.timestamp() - 20 * 86400
        fresh_ts = now.timestamp() - 2 * 86400
        creators = [
            {"username": "stale", "pushes_7d": 0, "last_push_ts": stale_ts},
            {"username": "fresh", "pushes_7d": 5, "last_push_ts": fresh_ts},
            {"username": "never", "pushes_7d": 0, "last_push_ts": None},
        ]
        text = _build_weekly_report(
            {"success": 1, "failure": 0, "skipped": 0}, creators, None, now=now
        )
        stale_line = next(l for l in text.splitlines() if "@stale" in l)
        never_line = next(l for l in text.splitlines() if "@never" in l)
        fresh_line = next(l for l in text.splitlines() if "@fresh" in l)
        assert "⚠️" in stale_line
        assert "⚠️" in never_line
        assert "⚠️" not in fresh_line

    def test_stale_boundary_exactly_14_days_is_not_flagged(self):
        now = FIXED_SUNDAY
        exactly = now.timestamp() - 14 * 86400
        a_bit_more = now.timestamp() - 14 * 86400 - 1
        creators_exact = [{"username": "a", "pushes_7d": 0, "last_push_ts": exactly}]
        creators_over = [{"username": "a", "pushes_7d": 0, "last_push_ts": a_bit_more}]
        stats = {"success": 0, "failure": 0, "skipped": 0}

        def _creator_line(creators):
            text = _build_weekly_report(stats, creators, "2026-09-12", now=now)
            return next(l for l in text.splitlines() if "@a:" in l)

        assert "⚠️" not in _creator_line(creators_exact)
        assert "⚠️" in _creator_line(creators_over)


# ---------------------------------------------------------------------------
# Push-history data source (state_store) + per-subscription collector
# ---------------------------------------------------------------------------


class TestPushHistoryDataSource:
    def test_roundtrip_record_and_load(self, tmp_path):
        pushed_dir = tmp_path / "seen_ids"
        recent = time.time() - 3600.0
        record_push_history(pushed_dir, "111", "alice", 42)
        record_push_history(pushed_dir, "111", "alice", 43, ts=recent)
        history = load_push_history(pushed_dir, "111", "alice")
        assert set(history) == {42, 43}
        assert history[42] == pytest.approx(time.time(), abs=60)
        assert history[43] == pytest.approx(recent, abs=2)

    def test_missing_file_loads_empty(self, tmp_path):
        assert load_push_history(tmp_path, "111", "nobody") == {}

    def test_corrupt_file_loads_empty(self, tmp_path):
        path = tmp_path / "push_history_111_corrupt.json"
        path.write_text("this is not json")
        assert load_push_history(tmp_path, "111", "corrupt") == {}

    def test_entries_older_than_retention_are_pruned(self, tmp_path):
        pushed_dir = tmp_path / "seen_ids"
        ancient = time.time() - 40 * 86400  # > PUSH_HISTORY_RETENTION_DAYS (35)
        record_push_history(pushed_dir, "111", "alice", 1, ts=ancient)
        # The ancient entry is pruned in the very same write.
        assert load_push_history(pushed_dir, "111", "alice") == {}
        record_push_history(pushed_dir, "111", "alice", 2)
        assert set(load_push_history(pushed_dir, "111", "alice")) == {2}

    def test_users_are_isolated_by_tg_id_and_username(self, tmp_path):
        record_push_history(tmp_path, "111", "alice", 1)
        record_push_history(tmp_path, "222", "alice", 2)
        assert set(load_push_history(tmp_path, "111", "alice")) == {1}
        assert set(load_push_history(tmp_path, "222", "alice")) == {2}


class TestCollectWeeklyCreatorStats:
    @staticmethod
    def _make_cfg(tmp_path: Path, usernames: list[str]):
        return civitai_bot.MonitorConfig(
            telegram={"bot_token": "UNSET", "chat_id": "111"},
            subscriptions={"111": [{"name": u} for u in usernames]},
            data={"data_dir": str(tmp_path)},
        )

    def test_window_counts_and_last_push(self, tmp_path):
        pushed_dir = tmp_path / "seen_ids"
        now = time.time()
        # Latest push (2 days ago, inside the window)...
        record_push_history(pushed_dir, "111", "alice", 1, ts=now - 2 * 86400)
        # ...plus an older one outside the 7-day window.
        record_push_history(pushed_dir, "111", "alice", 2, ts=now - 8 * 86400)
        stats = _collect_weekly_creator_stats(self._make_cfg(tmp_path, ["alice"]))
        assert len(stats) == 1
        assert stats[0]["username"] == "alice"
        assert stats[0]["pushes_7d"] == 1
        assert stats[0]["last_push_ts"] == pytest.approx(now - 2 * 86400, abs=2)

    def test_window_boundary_inclusive_at_cutoff(self, tmp_path):
        pushed_dir = tmp_path / "seen_ids"
        now = time.time()
        record_push_history(
            pushed_dir, "111", "alice", 1, ts=now - 7 * 86400 + 60
        )  # inside
        record_push_history(
            pushed_dir, "111", "alice", 2, ts=now - 7 * 86400 - 60
        )  # outside
        stats = _collect_weekly_creator_stats(self._make_cfg(tmp_path, ["alice"]))
        assert stats[0]["pushes_7d"] == 1

    def test_subscription_without_history_reports_none(self, tmp_path):
        stats = _collect_weekly_creator_stats(self._make_cfg(tmp_path, ["bob"]))
        assert stats[0]["pushes_7d"] == 0
        assert stats[0]["last_push_ts"] is None

    def test_creators_sorted_case_insensitively(self, tmp_path):
        stats = _collect_weekly_creator_stats(
            self._make_cfg(tmp_path, ["Zoe", "alice", "Bob"])
        )
        assert [s["username"] for s in stats] == ["alice", "Bob", "Zoe"]


# ---------------------------------------------------------------------------
# Scan-outcome counters in scheduled_scan_cron
# ---------------------------------------------------------------------------


class TestScanCounters:
    @staticmethod
    def _make_fake_proc(returncode: int):
        proc = MagicMock()
        proc.returncode = returncode
        proc.wait = AsyncMock(return_value=returncode)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    async def _run_scan_cron_once(self, monkeypatch, returncode: int | Exception):
        proc = self._make_fake_proc(returncode) if isinstance(returncode, int) else None
        spawn = (
            AsyncMock(return_value=proc)
            if proc is not None
            else AsyncMock(side_effect=returncode)
        )

        async def _sleep_then_shutdown(_seconds):
            civitai_bot._shutdown_requested = True

        monkeypatch.setattr(civitai_bot, "_load_active_backfills", dict)
        monkeypatch.setattr(civitai_bot, "_load_interval", lambda: 1)
        monkeypatch.setattr(civitai_bot, "_sweep_stale_backfills", lambda *_a: 0)
        monkeypatch.setattr(civitai_bot, "_report_cron_outcome", AsyncMock())
        monkeypatch.setattr(civitai_bot, "_last_scan_error_line", lambda: "")
        monkeypatch.setattr(
            civitai_bot,
            "read_config",
            lambda: MagicMock(backfill=MagicMock(stale_backfill_minutes=120)),
        )
        with patch.object(
            civitai_bot.asyncio, "create_subprocess_exec", spawn
        ), patch.object(
            civitai_bot.asyncio, "sleep", AsyncMock(side_effect=_sleep_then_shutdown)
        ):
            civitai_bot._shutdown_requested = False
            try:
                await scheduled_scan_cron()
            finally:
                civitai_bot._shutdown_requested = False

    @pytest.mark.asyncio
    async def test_exit_zero_counts_success(self, monkeypatch):
        await self._run_scan_cron_once(monkeypatch, 0)
        assert civitai_bot._scan_stats == {"success": 1, "failure": 0, "skipped": 0}

    @pytest.mark.asyncio
    async def test_exit_75_counts_skip_not_failure(self, monkeypatch):
        await self._run_scan_cron_once(monkeypatch, 75)
        assert civitai_bot._scan_stats == {"success": 0, "failure": 0, "skipped": 1}

    @pytest.mark.asyncio
    async def test_nonzero_exit_counts_failure(self, monkeypatch):
        await self._run_scan_cron_once(monkeypatch, 2)
        assert civitai_bot._scan_stats == {"success": 0, "failure": 1, "skipped": 0}

    @pytest.mark.asyncio
    async def test_spawn_exception_counts_failure(self, monkeypatch):
        await self._run_scan_cron_once(monkeypatch, RuntimeError("spawn boom"))
        assert civitai_bot._scan_stats == {"success": 0, "failure": 1, "skipped": 0}

    @pytest.mark.asyncio
    async def test_counters_accumulate_across_iterations(self, monkeypatch):
        await self._run_scan_cron_once(monkeypatch, 0)
        await self._run_scan_cron_once(monkeypatch, 75)
        await self._run_scan_cron_once(monkeypatch, 2)
        assert civitai_bot._scan_stats == {"success": 1, "failure": 1, "skipped": 1}


# ---------------------------------------------------------------------------
# Trigger-day decision
# ---------------------------------------------------------------------------


class TestWeeklyReportDue:
    @pytest.mark.parametrize(
        "now, last_sent, expected",
        [
            # Sunday 21:00 UTC sharp — fires.
            (datetime(2026, 9, 13, 21, 0, tzinfo=timezone.utc), None, True),
            # Later on Sunday evening — still fires (once per day via marker).
            (FIXED_SUNDAY, None, True),
            (datetime(2026, 9, 13, 23, 59, tzinfo=timezone.utc), None, True),
            # Too early on Sunday — no.
            (datetime(2026, 9, 13, 20, 59, tzinfo=timezone.utc), None, False),
            # Not Sunday — no (Saturday, Monday).
            (datetime(2026, 9, 12, 21, 5, tzinfo=timezone.utc), None, False),
            (datetime(2026, 9, 14, 21, 5, tzinfo=timezone.utc), None, False),
            # Already sent today — no.
            (FIXED_SUNDAY, "2026-09-13", False),
            # Sent last week — fires again.
            (FIXED_SUNDAY, "2026-09-06", True),
        ],
    )
    def test_due_matrix(self, now, last_sent, expected):
        assert _weekly_report_due(now, last_sent) is expected

    def test_fixed_fixture_date_is_actually_a_sunday(self):
        # Guard against silently breaking the whole parametrization above.
        assert FIXED_SUNDAY.weekday() == 6


# ---------------------------------------------------------------------------
# scheduled_weekly_report_cron — send, reset, marker, failure tolerance
# ---------------------------------------------------------------------------


class TestWeeklyReportCron:
    def _fake_bot(self):
        bot = MagicMock()
        bot.send_message = AsyncMock()
        return bot

    async def _run_cron_once(self, monkeypatch):
        async def _sleep_then_shutdown(_seconds):
            civitai_bot._shutdown_requested = True

        with patch.object(
            civitai_bot, "datetime", _FixedDatetime
        ), patch.object(
            civitai_bot.asyncio, "sleep", AsyncMock(side_effect=_sleep_then_shutdown)
        ):
            civitai_bot._shutdown_requested = False
            try:
                await scheduled_weekly_report_cron()
            finally:
                civitai_bot._shutdown_requested = False

    @pytest.mark.asyncio
    async def test_sends_on_sunday_resets_counters_and_sets_marker(self, monkeypatch):
        civitai_bot._alert_bot = self._fake_bot()
        civitai_bot._ADMIN_CHAT_IDS = [111]
        civitai_bot._reset_scan_stats()
        civitai_bot._scan_stats["success"] = 3
        civitai_bot._scan_stats["failure"] = 1
        civitai_bot._scan_stats["skipped"] = 2
        monkeypatch.setattr(
            civitai_bot,
            "_collect_weekly_creator_stats",
            lambda cfg=None: [
                {"username": "alice", "tg_id": "111", "pushes_7d": 2,
                 "last_push_ts": FIXED_SUNDAY.timestamp() - 3600},
            ],
        )
        monkeypatch.setattr(
            civitai_bot, "_load_reconciliation_last_success", lambda cfg=None: "2026-09-12"
        )

        await self._run_cron_once(monkeypatch)

        assert civitai_bot._alert_bot.send_message.await_count == 1
        kwargs = civitai_bot._alert_bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == 111
        text = kwargs["text"]
        assert "✅ 成功: 3 次" in text
        assert "❌ 失败: 1 次" in text
        assert "⏭️ 跳过: 2 次" in text
        assert "@alice: 近 7 天 2 条" in text
        assert "最近成功: 2026-09-12" in text
        # Counters are zeroed and the "sent today" marker is set.
        assert civitai_bot._scan_stats == {"success": 0, "failure": 0, "skipped": 0}
        assert civitai_bot._weekly_report_last_sent_date == "2026-09-13"

    @pytest.mark.asyncio
    async def test_does_not_send_twice_on_the_same_sunday(self, monkeypatch):
        civitai_bot._alert_bot = self._fake_bot()
        civitai_bot._ADMIN_CHAT_IDS = [111]
        civitai_bot._weekly_report_last_sent_date = "2026-09-13"

        await self._run_cron_once(monkeypatch)

        assert civitai_bot._alert_bot.send_message.await_count == 0

    @pytest.mark.asyncio
    async def test_no_bot_configured_is_a_noop(self, monkeypatch):
        civitai_bot._alert_bot = None
        civitai_bot._ADMIN_CHAT_IDS = []
        civitai_bot._scan_stats["success"] = 7

        await self._run_cron_once(monkeypatch)

        # Nothing sent, nothing consumed: counters survive for the next run.
        assert civitai_bot._scan_stats["success"] == 7
        assert civitai_bot._weekly_report_last_sent_date is None

    @pytest.mark.asyncio
    async def test_total_send_failure_keeps_counters_for_retry(self, monkeypatch):
        bot = self._fake_bot()
        bot.send_message = AsyncMock(side_effect=RuntimeError("telegram down"))
        civitai_bot._alert_bot = bot
        civitai_bot._ADMIN_CHAT_IDS = [111]
        civitai_bot._scan_stats["failure"] = 4
        monkeypatch.setattr(civitai_bot, "_collect_weekly_creator_stats", lambda cfg=None: [])
        monkeypatch.setattr(
            civitai_bot, "_load_reconciliation_last_success", lambda cfg=None: None
        )

        # Must not raise — a broken send never takes down the cron loop.
        await self._run_cron_once(monkeypatch)

        assert bot.send_message.await_count == 1
        assert civitai_bot._scan_stats == {"success": 0, "failure": 4, "skipped": 0}
        assert civitai_bot._weekly_report_last_sent_date is None


# ---------------------------------------------------------------------------
# Coverage gaps worth closing: the monitor -> push-history integration and
# load_push_history's tolerance of malformed/legacy files.
# ---------------------------------------------------------------------------


class TestRecordPushSuccessHistory:
    def test_push_success_records_history(self, tmp_path):
        """A confirmed push leaves a durable history entry (weekly-report feed)."""
        pushed_dir = tmp_path / "seen_ids"
        pushed_ids: set[int] = set()
        monitor_mod._record_push_success(
            42, "alice", pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id="111"
        )
        assert pushed_ids == {42}
        assert set(load_push_history(pushed_dir, "111", "alice")) == {42}

    def test_history_write_failure_never_fails_the_push(self, tmp_path, monkeypatch, caplog):
        """History is best-effort: StateWriteError must not fail a confirmed push."""
        pushed_dir = tmp_path / "seen_ids"
        pushed_ids: set[int] = set()

        def _boom(*_a, **_k):
            raise StateWriteError("lock timeout (simulated)")

        monkeypatch.setattr(monitor_mod, "record_push_history", _boom)
        monitor_mod._record_push_success(
            42, "alice", pushed_ids=pushed_ids, pushed_dir=pushed_dir, tg_id="111"
        )
        assert pushed_ids == {42}
        assert "Could not record push history" in caplog.text


class TestLoadPushHistoryTolerance:
    def test_non_dict_json_loads_empty(self, tmp_path):
        path = tmp_path / "push_history_111_shapelist.json"
        path.write_text("[1, 2, 3]")
        assert load_push_history(tmp_path, "111", "shapelist") == {}

    def test_legacy_dict_values_parse_ts(self, tmp_path):
        path = tmp_path / "push_history_111_legacy.json"
        path.write_text('{"7": {"ts": 1700000000.0}, "8": "nonsense"}')
        history = load_push_history(tmp_path, "111", "legacy")
        assert set(history) == {7}
        assert history[7] == 1700000000.0
