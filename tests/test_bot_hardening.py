"""Hardening tests for civitai-bot.py / bot_ui.py.

t1  heartbeat generation guard — a tick that fires after the finally block
    must neither re-register the backfill nor reschedule itself, while ticks
    fired during a live backfill keep refreshing on the 10s cadence.
t2  callback_data 64-byte limit — long usernames are short-hashed and
    round-trip via the in-process mapping; short usernames are untouched.
t3  /cleanup rejects days < 1 without touching the cache.
t4  cron failure alerts are deduplicated by state flip (one alert per failure
    episode, one daily digest), and a failing Telegram send never crashes the
    cron loop.
t5  _run_backfill rejects invalid usernames before any side effect.
"""

import asyncio
import importlib.util
import logging
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Load civitai-bot.py as a module (hyphenated name needs importlib) —
# same technique as tests/test_bot.py, distinct module name to avoid clashes.
_spec = importlib.util.spec_from_file_location(
    "civitai_bot_hardening_module", str(Path(__file__).parent.parent / "civitai-bot.py")
)
civitai_bot = importlib.util.module_from_spec(_spec)
sys.modules["civitai_bot_hardening_module"] = civitai_bot
_spec.loader.exec_module(civitai_bot)

import bot_ui


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


class _AlertBot:
    """Fake Telegram bot that records send_message calls."""

    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


@pytest.fixture
def alert_bot(monkeypatch):
    """Fresh alert dedup state + recording fake bot wired to admin chat 12345."""
    bot = _AlertBot()
    monkeypatch.setattr(civitai_bot, "_ADMIN_CHAT_IDS", [12345])
    monkeypatch.setattr(civitai_bot, "_alert_bot", bot)
    monkeypatch.setattr(civitai_bot, "_cron_alert_state", {})
    return bot


class _LoopSpy:
    """Delegating loop wrapper that records call_later() calls so tests can
    fire scheduled heartbeat callbacks manually instead of waiting 10s.

    Everything except call_later is delegated to the real running loop via
    __getattr__, so asyncio internals (wait_for/timeouts, task creation) keep
    working against the real loop.
    """

    def __init__(self, real):
        self._real = real
        self.scheduled = []  # list of (delay, callback, args)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def call_later(self, delay, callback, *args):
        self.scheduled.append((delay, callback, args))
        return self._real.call_later(delay, callback, *args)


def _make_instant_proc(returncode=0):
    """Subprocess mock whose stdout/stderr EOF immediately."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 1234
    proc.stdout = MagicMock()
    proc.stdout.read = AsyncMock(return_value=b"")
    proc.stderr = MagicMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    return proc


def _stub_backfill_locks(monkeypatch, tmp_path):
    monkeypatch.setattr(
        civitai_bot,
        "_acquire_backfill_lock",
        lambda tg, user: (42, tmp_path / f".lck_{tg}_{user}"),
    )
    monkeypatch.setattr(civitai_bot, "_release_backfill_lock", lambda fd, p: None)


def _isolate_backfill_state(monkeypatch, tmp_path):
    """Redirect backfill state files into tmp_path (never touch the repo)."""
    monkeypatch.setattr(civitai_bot, "SCRIPT_DIR", tmp_path)
    monkeypatch.setattr(civitai_bot, "ACTIVE_BACKFILLS", tmp_path / "active_backfills.json")


# ---------------------------------------------------------------------------
# t1 — heartbeat generation guard
# ---------------------------------------------------------------------------


class TestHeartbeatGenerationGuard:
    def _setup(self, monkeypatch, tmp_path):
        _isolate_backfill_state(monkeypatch, tmp_path)
        _stub_backfill_locks(monkeypatch, tmp_path)

    @pytest.mark.asyncio
    async def test_tick_after_finally_is_rejected(self, monkeypatch, tmp_path):
        """Race simulation: a tick fires after the finally block bumped the
        generation. Without the guard it would re-register AND re-arm —
        the heartbeat (and active_backfills.json freshness) would leak
        forever and scheduled scans would be suppressed until restart."""
        self._setup(monkeypatch, tmp_path)

        register_calls = []
        real_register = civitai_bot._register_backfill

        def _tracking_register(tg, user):
            register_calls.append((tg, user))
            real_register(tg, user)

        monkeypatch.setattr(civitai_bot, "_register_backfill", _tracking_register)

        proc = _make_instant_proc(0)
        create_subproc = AsyncMock(return_value=proc)
        monkeypatch.setattr(civitai_bot.asyncio, "create_subprocess_exec", create_subproc)

        spy = _LoopSpy(asyncio.get_running_loop())
        with patch.object(civitai_bot.asyncio, "get_running_loop", return_value=spy):
            await civitai_bot._run_backfill("alice", 111)

        # finally ran: registry cleared
        assert civitai_bot._load_active_backfills() == {}
        # Exactly one tick armed at entry: 10s cadence, generation 0.
        assert len(spy.scheduled) == 1
        delay, tick, args = spy.scheduled[0]
        assert delay == 10.0
        assert args == (0,)

        # Fire the tick AFTER the coroutine exited (the dangerous race).
        tick(*args)
        # Only the initial registration happened; the stale tick must not
        # register again nor re-arm the heartbeat.
        assert register_calls == [("111", "alice")]
        assert len(spy.scheduled) == 1

    @pytest.mark.asyncio
    async def test_heartbeat_keeps_refreshing_while_running(self, monkeypatch, tmp_path):
        """While the backfill is alive (generation unchanged), ticks keep the
        10s cadence, refresh active_backfills.json, and re-arm themselves."""
        self._setup(monkeypatch, tmp_path)

        release = asyncio.Event()
        proc = MagicMock()
        proc.returncode = 0
        proc.pid = 1234

        async def _block_until_release(*_a, **_k):
            await release.wait()
            return b""

        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=_block_until_release)
        proc.stderr = MagicMock()
        proc.stderr.read = AsyncMock(side_effect=_block_until_release)

        monkeypatch.setattr(
            civitai_bot.asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)
        )

        real_loop = asyncio.get_running_loop()
        spy = _LoopSpy(real_loop)
        with patch.object(civitai_bot.asyncio, "get_running_loop", return_value=spy):
            task = real_loop.create_task(civitai_bot._run_backfill("alice", 111))
            await asyncio.sleep(0.05)  # let the task arm the heartbeat

            assert len(spy.scheduled) == 1
            delay, tick, args = spy.scheduled[0]
            assert delay == 10.0 and args == (0,)
            assert civitai_bot._load_active_backfills()  # registered at entry

            tick(*args)  # fires while the backfill is still running
            assert len(spy.scheduled) == 2  # re-armed, same generation
            assert spy.scheduled[1][0] == 10.0
            assert spy.scheduled[1][2] == (0,)
            assert civitai_bot._load_active_backfills()  # still registered

        release.set()
        await task
        assert civitai_bot._load_active_backfills() == {}  # finally cleanup

        # The last tick, fired after exit, must not re-arm either.
        _, tick2, args2 = spy.scheduled[1]
        tick2(*args2)
        assert len(spy.scheduled) == 2


# ---------------------------------------------------------------------------
# t2 — callback_data 64-byte limit (short hash + mapping)
# ---------------------------------------------------------------------------


class TestCallbackDataLimit:
    @pytest.fixture(autouse=True)
    def _clean_mapping(self, monkeypatch):
        monkeypatch.setattr(bot_ui, "_CALLBACK_HASH_TO_USERNAME", {})

    def _keyboard(self, users, prefix="rem"):
        markup, _text, _page = bot_ui.paginated_user_keyboard(
            users,
            0,
            item_prefix=prefix,
            item_label_fmt="❌ @{u}",
            page_prefix=f"{prefix}_pg",
            close_data=f"{prefix}_cl",
        )
        return markup

    def test_64_char_username_hashed_and_within_limit(self):
        long_user = "u" * 64  # USERNAME_RE allows up to 64 chars
        markup = self._keyboard([long_user])
        data = markup.inline_keyboard[0][0].callback_data
        assert len(data.encode("utf-8")) <= 64
        seg = data.split(":", 1)[1]
        assert seg != long_user  # must be the short hash, not the raw name
        assert len(seg) == 12
        # Round-trip: the callback consumer resolves it back to the username.
        assert bot_ui.resolve_callback_username(seg) == long_user

    def test_short_username_unchanged(self):
        markup = self._keyboard(["alice"])
        assert markup.inline_keyboard[0][0].callback_data == "rem:alice"
        assert bot_ui.resolve_callback_username("alice") == "alice"

    def test_resolve_unknown_segment_passthrough(self):
        # Unknown segments (older process, foreign data) pass through so the
        # consumer can fall back to exact-name matching.
        assert bot_ui.resolve_callback_username("not_a_known_hash") == "not_a_known_hash"

    def test_all_buttons_within_64_bytes(self):
        users = ["u" * 64, "a" * 40, "alice"]
        markup = self._keyboard(users, prefix="bf")
        for row in markup.inline_keyboard:
            for btn in row:
                assert len(btn.callback_data.encode("utf-8")) <= 64

    def test_repeated_render_keeps_mapping_stable(self):
        long_user = "w" * 64
        m1 = self._keyboard([long_user])
        m2 = self._keyboard([long_user])
        assert m1.inline_keyboard[0][0].callback_data == m2.inline_keyboard[0][0].callback_data


# ---------------------------------------------------------------------------
# t3 — /cleanup rejects days < 1
# ---------------------------------------------------------------------------


class TestCleanupDaysValidation:
    AUTH_ID = 8628596870

    def _make_update(self, text):
        update = MagicMock()
        update.effective_user.id = self.AUTH_ID
        update.message.text = text
        update.message.reply_text = AsyncMock()
        return update

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["0", "-1"])
    async def test_cleanup_rejects_non_positive_days(self, monkeypatch, bad):
        monkeypatch.setattr(civitai_bot, "AUTHORIZED_USER_IDS", {self.AUTH_ID})
        cleanup_mock = MagicMock()
        monkeypatch.setattr(civitai_bot, "cleanup_old_caches", cleanup_mock)
        update = self._make_update(f"/cleanup {bad}")

        await civitai_bot.cmd_cleanup(update, None)

        update.message.reply_text.assert_awaited_once()
        assert "Usage" in update.message.reply_text.call_args.args[0]
        cleanup_mock.assert_not_called()  # nothing was deleted

    @pytest.mark.asyncio
    async def test_cleanup_accepts_positive_days(self, monkeypatch, tmp_path):
        monkeypatch.setattr(civitai_bot, "AUTHORIZED_USER_IDS", {self.AUTH_ID})
        monkeypatch.setattr(civitai_bot, "DOWNLOAD_DIR", tmp_path)
        cleanup_mock = MagicMock(return_value=0)
        monkeypatch.setattr(civitai_bot, "cleanup_old_caches", cleanup_mock)
        update = self._make_update("/cleanup 3")

        await civitai_bot.cmd_cleanup(update, None)

        cleanup_mock.assert_called_once()
        assert cleanup_mock.call_args.args[1] == 3


# ---------------------------------------------------------------------------
# t4 — cron failure alerts with state-flip dedup
# ---------------------------------------------------------------------------


class TestCronAlertDedup:
    def _make_failing_proc(self, returncode=1):
        proc = MagicMock()
        proc.returncode = returncode
        proc.pid = 4321
        proc.wait = AsyncMock(return_value=returncode)
        proc.terminate = MagicMock()
        proc.kill = MagicMock()
        return proc

    async def _run_scan_cron_failures(self, monkeypatch, tmp_path, iterations=2):
        """Run scheduled_scan_cron through `iterations` failing cycles."""
        _isolate_backfill_state(monkeypatch, tmp_path)
        monkeypatch.setattr(civitai_bot, "_load_active_backfills", lambda: {})
        create_subproc = AsyncMock(return_value=self._make_failing_proc(1))
        n = [0]

        async def _fake_sleep(*_a, **_k):
            n[0] += 1
            if n[0] >= iterations:
                civitai_bot._shutdown_requested = True

        with patch.object(
            civitai_bot.asyncio, "create_subprocess_exec", create_subproc
        ), patch.object(civitai_bot.asyncio, "sleep", AsyncMock(side_effect=_fake_sleep)):
            civitai_bot._shutdown_requested = False
            try:
                await civitai_bot.scheduled_scan_cron()
            finally:
                civitai_bot._shutdown_requested = False
        return create_subproc

    @pytest.mark.asyncio
    async def test_consecutive_failures_alert_once(self, monkeypatch, tmp_path, alert_bot):
        """Two failing scan cycles in a row -> exactly ONE Telegram alert."""
        create_subproc = await self._run_scan_cron_failures(
            monkeypatch, tmp_path, iterations=2
        )
        assert create_subproc.await_count == 2  # both cycles ran and failed
        assert len(alert_bot.sent) == 1
        chat_id, text = alert_bot.sent[0]
        assert chat_id == 12345
        assert "定时扫描失败" in text
        assert "exit 1" in text

    @pytest.mark.asyncio
    async def test_send_failure_does_not_crash_cron(self, monkeypatch, tmp_path, alert_bot):
        """A raising bot.send_message must not propagate into the cron loop."""

        async def _boom(*_a, **_k):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(alert_bot, "send_message", _boom)
        create_subproc = await self._run_scan_cron_failures(
            monkeypatch, tmp_path, iterations=2
        )
        assert create_subproc.await_count == 2  # cron completed both cycles

    @pytest.mark.asyncio
    async def test_gate_state_flips(self, alert_bot):
        """fail -> recover -> fail cycle, all decided in-process."""
        gate = civitai_bot._cron_alert_gate
        assert gate("scan", failing=True) == "fail"
        assert gate("scan", failing=True) is None  # consecutive failure silent
        assert gate("scan", failing=False) == "recover"
        assert gate("scan", failing=False) is None
        assert gate("scan", failing=True) == "fail"  # new episode alerts again
        assert len(alert_bot.sent) == 0  # the gate only decides; sends are separate

    @pytest.mark.asyncio
    async def test_daily_digest_for_persistent_failure(self, alert_bot):
        gate = civitai_bot._cron_alert_gate
        assert gate("scan", failing=True, today="2026-09-10") == "fail"
        assert gate("scan", failing=True, today="2026-09-10") is None
        assert gate("scan", failing=True, today="2026-09-11") == "digest"
        assert gate("scan", failing=True, today="2026-09-11") is None

    @pytest.mark.asyncio
    async def test_report_cron_outcome_end_to_end(self, alert_bot):
        """_report_cron_outcome sends the fail alert once, then recovery once."""
        await civitai_bot._report_cron_outcome("reconciliation", failing=True, detail="exit 2")
        await civitai_bot._report_cron_outcome("reconciliation", failing=True, detail="exit 2")
        assert len(alert_bot.sent) == 1
        assert "每日对账失败" in alert_bot.sent[0][1]
        assert "exit 2" in alert_bot.sent[0][1]
        await civitai_bot._report_cron_outcome("reconciliation", failing=False)
        assert len(alert_bot.sent) == 2
        assert "已恢复" in alert_bot.sent[1][1]

    @pytest.mark.asyncio
    async def test_reconciliation_failure_alerts(self, monkeypatch, tmp_path, alert_bot):
        """scheduled_reconciliation_cron failure path pages admins once."""
        _isolate_backfill_state(monkeypatch, tmp_path)
        monkeypatch.setattr(civitai_bot, "_load_active_backfills", lambda: {})
        monkeypatch.setattr(civitai_bot, "_load_reconciliation_last_success", lambda *a: None)
        monkeypatch.setattr(civitai_bot, "_save_reconciliation_last_success", lambda *a, **k: None)

        cfg = MagicMock()
        cfg.reconciliation.enabled = True
        cfg.reconciliation.time = "03:30"
        monkeypatch.setattr(civitai_bot, "read_config", lambda: cfg)

        create_subproc = AsyncMock(return_value=self._make_failing_proc(1))
        n = [0]

        async def _fake_sleep(*_a, **_k):
            n[0] += 1
            if n[0] >= 2:
                civitai_bot._shutdown_requested = True

        fake_dt = datetime(2026, 9, 11, 4, 5, 0)
        mock_dt = MagicMock()
        mock_dt.now.return_value = fake_dt
        mock_dt.fromisoformat = datetime.fromisoformat

        with patch.object(civitai_bot, "datetime", mock_dt), patch.object(
            civitai_bot.asyncio, "create_subprocess_exec", create_subproc
        ), patch.object(civitai_bot.asyncio, "sleep", AsyncMock(side_effect=_fake_sleep)):
            civitai_bot._shutdown_requested = False
            try:
                await civitai_bot.scheduled_reconciliation_cron()
            finally:
                civitai_bot._shutdown_requested = False

        assert create_subproc.await_count == 2
        assert len(alert_bot.sent) == 1  # consecutive failures dedup to one
        assert "每日对账失败" in alert_bot.sent[0][1]


# ---------------------------------------------------------------------------
# t5 — _run_backfill rejects invalid usernames
# ---------------------------------------------------------------------------


class TestRunBackfillInputValidation:
    @pytest.mark.asyncio
    async def test_invalid_username_rejected_before_side_effects(
        self, monkeypatch, tmp_path, caplog
    ):
        _isolate_backfill_state(monkeypatch, tmp_path)
        acquire = MagicMock(return_value=(42, tmp_path / ".lck"))
        release = MagicMock()
        monkeypatch.setattr(civitai_bot, "_acquire_backfill_lock", acquire)
        monkeypatch.setattr(civitai_bot, "_release_backfill_lock", release)
        create_subproc = AsyncMock()
        monkeypatch.setattr(civitai_bot.asyncio, "create_subprocess_exec", create_subproc)

        bad_names = ["../escape", "a", "", "bad name", "u" * 65, "列", None]
        with caplog.at_level(logging.ERROR, logger="civitai-bot"):
            for bad in bad_names:
                result = await civitai_bot._run_backfill(bad, 111)
                assert result is None, f"expected None for {bad!r}"

        create_subproc.assert_not_called()  # no subprocess ever spawned
        acquire.assert_not_called()  # no lock file touched
        release.assert_not_called()
        assert civitai_bot._load_active_backfills() == {}  # registry untouched
        assert any("invalid username" in r.getMessage() for r in caplog.records)
