"""Tests for civitai-bot.py command-parsing and helper logic."""

import asyncio
import importlib.util
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pathlib import Path

# Load civitai-bot.py as a module (hyphenated name needs importlib)
_spec = importlib.util.spec_from_file_location(
    "civitai_bot_module", str(Path(__file__).parent.parent / "civitai-bot.py")
)
civitai_bot = importlib.util.module_from_spec(_spec)
sys.modules["civitai_bot_module"] = civitai_bot
_spec.loader.exec_module(civitai_bot)

from civitai_bot_module import (  # noqa: E402
    MonitorConfig,
    _CIVITAI_URL_PREFIXES,
    get_users,
    parse_username_input,
    scheduled_scan_cron,
    set_users,
)


# ---------------------------------------------------------------------------
# _CIVITAI_URL_PREFIXES
# ---------------------------------------------------------------------------

class TestCivitaiUrlPrefixes:
    def test_contains_com_and_red(self):
        assert "https://civitai.com/user/" in _CIVITAI_URL_PREFIXES
        assert "https://civitai.red/user/" in _CIVITAI_URL_PREFIXES

    def test_has_www_variants(self):
        assert "https://www.civitai.com/user/" in _CIVITAI_URL_PREFIXES
        assert "https://www.civitai.red/user/" in _CIVITAI_URL_PREFIXES


# ---------------------------------------------------------------------------
# parse_username_input
# ---------------------------------------------------------------------------

class TestParseUsernameInput:
    def test_plain_username(self):
        assert parse_username_input("alice") == "alice"

    def test_at_prefix(self):
        assert parse_username_input("@bob") == "bob"

    def test_civitai_url_https(self):
        assert parse_username_input("https://civitai.com/user/carol") == "carol"

    def test_civitai_url_http(self):
        assert parse_username_input("http://civitai.com/user/dave") == "dave"

    def test_www_civitai_url(self):
        assert parse_username_input("https://www.civitai.com/user/eve") == "eve"

    def test_civitai_red_url(self):
        assert parse_username_input("https://civitai.red/user/frank") == "frank"

    def test_www_civitai_red_url(self):
        assert parse_username_input("https://www.civitai.red/user/grace") == "grace"

    def test_strips_whitespace(self):
        assert parse_username_input("  alice  ") == "alice"

    def test_url_with_query_params_strips_after_username(self):
        """Username is extracted even if URL has trailing ?foo=bar."""
        assert parse_username_input("https://civitai.com/user/alice?ref=bar") == "alice"

    def test_unknown_domain_returns_none(self):
        assert parse_username_input("https://evil.com/user/hacker") is None

    def test_totally_invalid_returns_none(self):
        assert parse_username_input("!!!") is None

    def test_empty_string_returns_none(self):
        assert parse_username_input("") is None


# ---------------------------------------------------------------------------
# get_users / set_users
# ---------------------------------------------------------------------------

class TestGetSetUsers:
    @pytest.fixture
    def cfg(self):
        return MonitorConfig(
            telegram={"bot_token": "t", "chat_id": "c"},
            subscriptions={},
            authorized_users=[],
        )

    def test_get_users_empty(self, cfg):
        assert get_users(cfg, telegram_user_id=111) == []

    def test_set_users_then_get(self, cfg):
        cfg = set_users(cfg, telegram_user_id=111, users=["alice", "bob"])
        assert get_users(cfg, telegram_user_id=111) == ["alice", "bob"]

    def test_set_users_overwrites(self, cfg):
        cfg = set_users(cfg, telegram_user_id=111, users=["alice"])
        cfg = set_users(cfg, telegram_user_id=111, users=["bob"])
        assert get_users(cfg, telegram_user_id=111) == ["bob"]

    def test_set_users_multiple_telegram_users(self, cfg):
        cfg = set_users(cfg, telegram_user_id=111, users=["alice"])
        cfg = set_users(cfg, telegram_user_id=222, users=["bob"])
        assert get_users(cfg, telegram_user_id=111) == ["alice"]
        assert get_users(cfg, telegram_user_id=222) == ["bob"]


# ---------------------------------------------------------------------------
# scheduled_scan_cron — lifecycle / shutdown / lock contention
# ---------------------------------------------------------------------------


class TestScheduledScanCron:
    """Verify the cron loop's critical properties:

    1. Must NOT call ``os._exit`` (would bypass PTB's clean shutdown).
    2. Must skip on active backfill without spawning monitor.py.
    3. Must treat monitor.py exit 75 (lock held) as "skipped", not "failed".
    4. Must terminate a still-running subprocess when shutdown is requested.
    """

    def _make_fake_proc(self, returncode: int | None):
        proc = MagicMock()
        proc.returncode = returncode
        # asyncio.subprocess.Process.wait() returns the int returncode.
        # None means "still running" — we model that by returning -1 sentinel
        # (real asyncio wouldn't return None here; we coerce to int below).
        if returncode is None:
            proc.wait = AsyncMock(return_value=None)
        else:
            proc.wait = AsyncMock(return_value=returncode)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    def _patch_active_backfills(self, monkeypatch, data):
        """Stub the file read so we don't touch real disk."""
        monkeypatch.setattr(
            civitai_bot, "_load_active_backfills", lambda: data
        )

    @pytest.mark.asyncio
    async def test_no_os_exit_on_clean_loop_exit(self, monkeypatch):
        """Cron must return normally; os._exit would tear down the bot."""
        self._patch_active_backfills(monkeypatch, {})

        # Make subprocess "exit immediately" with rc 0 then set shutdown flag
        proc = self._make_fake_proc(returncode=0)
        create_subproc = AsyncMock(return_value=proc)
        sleep_mock = AsyncMock(side_effect=lambda *_a, **_k: civitai_bot.__class__)

        with patch.object(
            civitai_bot.asyncio, "create_subprocess_exec", create_subproc
        ), patch.object(civitai_bot.asyncio, "sleep", sleep_mock):
            # Set shutdown before the first iteration so the loop exits
            civitai_bot._shutdown_requested = True
            try:
                await scheduled_scan_cron()
            finally:
                civitai_bot._shutdown_requested = False

        # The cron must have returned (not os._exit) — the test running
        # at all proves this. Also, no subprocess was spawned because
        # we set the shutdown flag before the loop body ran.
        assert create_subproc.await_count == 0

    @pytest.mark.asyncio
    async def test_skips_when_active_backfill_present(self, monkeypatch):
        """If a fresh backfill is registered, cron must NOT spawn monitor.py."""
        # Use a timestamp within the stale-watchdog window (default 120 min)
        # so the entry is treated as a real, ongoing backfill — not a zombie
        # that the watchdog should sweep away.
        from datetime import datetime, timezone, timedelta
        fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self._patch_active_backfills(monkeypatch, {"111": {"alice": fresh}})
        create_subproc = AsyncMock()
        sleep_mock = AsyncMock()

        def _set_shutdown(*_a, **_k):
            civitai_bot._shutdown_requested = True

        sleep_mock.side_effect = _set_shutdown

        with patch.object(
            civitai_bot.asyncio, "create_subprocess_exec", create_subproc
        ), patch.object(civitai_bot.asyncio, "sleep", sleep_mock):
            civitai_bot._shutdown_requested = False
            try:
                await scheduled_scan_cron()
            finally:
                civitai_bot._shutdown_requested = False

        create_subproc.assert_not_called()

    @pytest.mark.asyncio
    async def test_sweeps_stale_active_backfill_and_proceeds(self, monkeypatch):
        """Stale backfill (>2h) must be cleared by the watchdog so the scan runs.

        Regression test for the 8-hour deadlock incident: a bot killed -9
        mid-backfill would leave active_backfills.json populated forever,
        permanently blocking the scan cron.
        """
        from datetime import datetime, timezone, timedelta
        ancient = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        self._patch_active_backfills(monkeypatch, {"111": {"alice": ancient}})
        proc = self._make_fake_proc(returncode=0)
        create_subproc = AsyncMock(return_value=proc)

        iterations = [0]

        def _maybe_shutdown(*_a, **_k):
            iterations[0] += 1
            if iterations[0] >= 1:
                civitai_bot._shutdown_requested = True

        sleep_mock = AsyncMock(side_effect=_maybe_shutdown)

        with patch.object(
            civitai_bot.asyncio, "create_subprocess_exec", create_subproc
        ), patch.object(civitai_bot.asyncio, "sleep", sleep_mock):
            civitai_bot._shutdown_requested = False
            try:
                await scheduled_scan_cron()
            finally:
                civitai_bot._shutdown_requested = False

        # Watchdog should have removed the stale entry, so monitor.py ran
        create_subproc.assert_called_once()
        # And active_backfills should now be empty
        assert civitai_bot._load_active_backfills() == {}

    @pytest.mark.asyncio
    async def test_returncode_75_treated_as_skip_not_failure(self, monkeypatch, caplog):
        """monitor.py exits 75 when .monitor.lock is held (backfill won).

        Cron should log it as "skipped" — NOT as "failed" — so operators
        aren't paged for normal backfill contention.
        """
        import logging
        self._patch_active_backfills(monkeypatch, {})

        proc = self._make_fake_proc(returncode=75)
        create_subproc = AsyncMock(return_value=proc)

        iterations = [0]

        def _maybe_shutdown(*_a, **_k):
            iterations[0] += 1
            if iterations[0] >= 1:
                civitai_bot._shutdown_requested = True

        sleep_mock = AsyncMock(side_effect=_maybe_shutdown)

        with caplog.at_level(logging.INFO, logger="civitai-bot"):
            with patch.object(
                civitai_bot.asyncio, "create_subprocess_exec", create_subproc
            ), patch.object(civitai_bot.asyncio, "sleep", sleep_mock):
                civitai_bot._shutdown_requested = False
                try:
                    await scheduled_scan_cron()
                finally:
                    civitai_bot._shutdown_requested = False

        # The subproc was spawned (backfills were empty)
        create_subproc.assert_called_once()
        # And we logged "skipped" for exit 75, NOT "failed"
        messages = [r.getMessage() for r in caplog.records]
        skipped_logs = [m for m in messages if "skipped" in m.lower()]
        failed_logs = [m for m in messages if "failed" in m.lower()]
        assert skipped_logs, f"Expected a 'skipped' log, got: {messages}"
        assert not failed_logs, f"Did not expect a 'failed' log, got: {messages}"

    @pytest.mark.asyncio
    async def test_terminates_running_subproc_on_shutdown(self, monkeypatch):
        """If shutdown is requested while monitor.py is running, it must be
        SIGTERM'd (not left to leak)."""
        self._patch_active_backfills(monkeypatch, {})

        proc = self._make_fake_proc(returncode=None)  # still running
        create_subproc = AsyncMock(return_value=proc)

        async def _fake_wait():
            # Simulate a long-running scan: set shutdown from "outside"
            civitai_bot._shutdown_requested = True
            return None

        proc.wait = AsyncMock(side_effect=_fake_wait)
        sleep_mock = AsyncMock()

        with patch.object(
            civitai_bot.asyncio, "create_subprocess_exec", create_subproc
        ), patch.object(civitai_bot.asyncio, "sleep", sleep_mock):
            civitai_bot._shutdown_requested = False
            try:
                await scheduled_scan_cron()
            finally:
                civitai_bot._shutdown_requested = False

        proc.terminate.assert_called_once()
        proc.wait.assert_called()

    @pytest.mark.asyncio
    async def test_silently_swallows_process_lookup_error_on_shutdown(self, monkeypatch):
        """If the subproc already exited (race), terminate() raises ProcessLookupError.
        The cron must not crash; it should log and return."""
        self._patch_active_backfills(monkeypatch, {})

        proc = self._make_fake_proc(returncode=None)
        proc.terminate.side_effect = ProcessLookupError(123, "No such process")
        create_subproc = AsyncMock(return_value=proc)

        async def _fake_wait():
            civitai_bot._shutdown_requested = True
            return None
        proc.wait = AsyncMock(side_effect=_fake_wait)
        sleep_mock = AsyncMock()

        with patch.object(
            civitai_bot.asyncio, "create_subprocess_exec", create_subproc
        ), patch.object(civitai_bot.asyncio, "sleep", sleep_mock):
            civitai_bot._shutdown_requested = False
            try:
                await scheduled_scan_cron()  # must not raise
            finally:
                civitai_bot._shutdown_requested = False

        proc.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_survives_closed_event_loop_during_shutdown_cleanup(self, monkeypatch, caplog):
        """Regression: when PTB's Application.stop() closes the event loop
        while scheduled_scan_cron is still inside `await proc.wait()`, the
        finally-block's `asyncio.wait_for(proc.wait(), timeout=30)` raises
        ``RuntimeError: no running event loop``. The cron must catch it,
        NOT propagate, and return cleanly (otherwise the coroutine leaks
        and Python logs ``Exception ignored in: <coroutine ...>`` during GC).

        This simulates the real production sequence we saw on 2026-06-06:
        the bot was SIGTERM'd mid-scan, the loop got closed before the
        finally ran, and ``asyncio.wait_for`` blew up.
        """
        import logging
        self._patch_active_backfills(monkeypatch, {})

        proc = self._make_fake_proc(returncode=None)  # still running
        create_subproc = AsyncMock(return_value=proc)

        async def _fake_wait():
            # Simulate the scan noticing shutdown was requested
            civitai_bot._shutdown_requested = True
            return None
        proc.wait = AsyncMock(side_effect=_fake_wait)
        sleep_mock = AsyncMock()

        # In the finally block, asyncio.wait_for() will be called. Model
        # the closed-loop state by having it raise RuntimeError exactly
        # like real CPython 3.13 does (asyncio.timeout() -> get_running_loop()).
        async def _wait_for_raises_no_loop(*_a, **_k):
            raise RuntimeError("no running event loop")

        with caplog.at_level(logging.INFO, logger="civitai-bot"):
            with patch.object(
                civitai_bot.asyncio, "create_subprocess_exec", create_subproc
            ), patch.object(
                civitai_bot.asyncio, "wait_for", _wait_for_raises_no_loop
            ), patch.object(civitai_bot.asyncio, "sleep", sleep_mock):
                civitai_bot._shutdown_requested = False
                try:
                    await scheduled_scan_cron()  # MUST NOT RAISE
                finally:
                    civitai_bot._shutdown_requested = False

        # terminate() was called (SIGTERM sent to subprocess)
        proc.terminate.assert_called_once()
        # kill() was NOT called (we don't escalate if wait_for can't be used)
        proc.kill.assert_not_called()
        # We logged the loop-closed fallback
        messages = [r.getMessage() for r in caplog.records]
        assert any("event loop already closed" in m for m in messages), (
            f"Expected a 'event loop already closed' log, got: {messages}"
        )


# ---------------------------------------------------------------------------
# _run_backfill — serial lock, finally cleanup, cross-process lock
# Regression tests for the 8-hour deadlock that motivated the rewrite.
# ---------------------------------------------------------------------------


class TestRunBackfillRegression:
    """Pin down the invariants the deadlock fix relies on."""

    def test_serial_lock_is_singleton(self):
        """The asyncio.Lock must be a process-wide singleton; otherwise
        concurrent backfills within the same bot can race."""
        lock1 = civitai_bot._get_backfill_serial_lock()
        lock2 = civitai_bot._get_backfill_serial_lock()
        assert lock1 is lock2

    def test_acquire_and_release_backfill_lock(self, tmp_path, monkeypatch):
        """Cross-process lock: acquire → second acquire fails → release → succeeds again."""
        # Redirect SCRIPT_DIR so the .lck file lands in tmp_path
        monkeypatch.setattr(civitai_bot, "SCRIPT_DIR", tmp_path)
        result1 = civitai_bot._acquire_backfill_lock("111", "alice")
        assert result1 is not None
        fd, path = result1
        # Second acquire on the same path must fail
        result2 = civitai_bot._acquire_backfill_lock("111", "alice")
        assert result2 is None
        # Release and re-acquire works
        civitai_bot._release_backfill_lock(fd, path)
        result3 = civitai_bot._acquire_backfill_lock("111", "alice")
        assert result3 is not None
        civitai_bot._release_backfill_lock(*result3)

    @pytest.mark.asyncio
    async def test_run_backfill_clears_active_state_in_finally(self, monkeypatch, tmp_path):
        """No matter how _run_backfill exits, active_backfills.json must be empty."""
        monkeypatch.setattr(civitai_bot, "SCRIPT_DIR", tmp_path)
        monkeypatch.setattr(civitai_bot, "ACTIVE_BACKFILLS", tmp_path / "active_backfills.json")

        # Pre-populate active_backfills to simulate "backfill starting"
        civitai_bot._register_backfill("111", "alice")

        # Mock the subprocess to return success. The real _run_backfill reads
        # proc.stdout / proc.stderr via `await stream.read(4096)` inside
        # communicate_with_idle_timeout, so those streams must be AsyncMock —
        # a plain MagicMock cannot be awaited and would raise TypeError.
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 12345
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(return_value=b"")  # EOF -> reader loop ends
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")

        async def _no_launch(*_a, **_k):
            return proc

        # Patch _launch (was a closure; replace _run_backfill's subprocess call)
        monkeypatch.setattr(
            civitai_bot.asyncio, "create_subprocess_exec", _no_launch
        )

        # Bypass cross-process lock by stubbing it to always succeed
        monkeypatch.setattr(
            civitai_bot, "_acquire_backfill_lock",
            lambda tg, user: (42, tmp_path / f".lck_{tg}_{user}")
        )
        monkeypatch.setattr(
            civitai_bot, "_release_backfill_lock", lambda fd, p: None
        )

        result = await civitai_bot._run_backfill("alice", 111)
        assert result is not None
        assert civitai_bot._load_active_backfills() == {}  # finally cleanup

    @pytest.mark.asyncio
    async def test_run_backfill_unregisters_on_subprocess_error(self, monkeypatch, tmp_path):
        """If the subprocess raises, active_backfills is still cleared."""
        monkeypatch.setattr(civitai_bot, "SCRIPT_DIR", tmp_path)
        monkeypatch.setattr(civitai_bot, "ACTIVE_BACKFILLS", tmp_path / "active_backfills.json")

        civitai_bot._register_backfill("111", "alice")

        async def _boom(*_a, **_k):
            raise OSError("simulated spawn failure")

        monkeypatch.setattr(civitai_bot.asyncio, "create_subprocess_exec", _boom)
        monkeypatch.setattr(
            civitai_bot, "_acquire_backfill_lock",
            lambda tg, user: (42, tmp_path / f".lck_{tg}_{user}")
        )
        monkeypatch.setattr(
            civitai_bot, "_release_backfill_lock", lambda fd, p: None
        )

        with pytest.raises(OSError):
            await civitai_bot._run_backfill("alice", 111)
        assert civitai_bot._load_active_backfills() == {}

    @pytest.mark.asyncio
    async def test_run_backfill_clears_lock_after_timeout(self, monkeypatch, tmp_path):
        """If the subprocess hangs past 2h, we kill it and still clear state."""
        monkeypatch.setattr(civitai_bot, "SCRIPT_DIR", tmp_path)
        monkeypatch.setattr(civitai_bot, "ACTIVE_BACKFILLS", tmp_path / "active_backfills.json")
        civitai_bot._register_backfill("111", "alice")

        # Subprocess that "hangs": it never produces output and never exits.
        # communicate_with_idle_timeout spins up reader tasks that `await
        # stream.read(4096)`, so the stream mocks must be AsyncMock.
        proc = MagicMock()
        proc.returncode = None  # still running
        proc.pid = 99999
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(return_value=b"")  # readers terminate cleanly
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(return_value=b"")
        proc.wait = AsyncMock(return_value=None)

        async def _no_launch(*_a, **_k):
            return proc

        monkeypatch.setattr(civitai_bot.asyncio, "create_subprocess_exec", _no_launch)
        # The genuine idle-timeout is asyncio.wait_for(queue.get(), timeout=1800).
        # Compress it to fire immediately so we exercise the real TimeoutError
        # path (kill -> finally cleanup) without a 30-minute wait.
        monkeypatch.setattr(
            civitai_bot.asyncio, "wait_for",
            AsyncMock(side_effect=asyncio.TimeoutError()),
        )
        monkeypatch.setattr(
            civitai_bot, "_acquire_backfill_lock",
            lambda tg, user: (42, tmp_path / f".lck_{tg}_{user}")
        )

        released_paths = []
        def _tracking_release(fd, p):
            released_paths.append(p)
        monkeypatch.setattr(civitai_bot, "_release_backfill_lock", _tracking_release)

        result = await civitai_bot._run_backfill("alice", 111)
        assert result is None  # timeout sentinel
        assert civitai_bot._load_active_backfills() == {}
        assert len(released_paths) == 1  # lock file released
