"""Targeted tests for the round-4 audit small items.

t1 — civitai_client.fetch_page: non-list `items` raises FetchPageError,
     non-dict elements are filtered, normal payloads unchanged.
t2 — cmd_scan registers its subprocess in _current_scan_proc so cmd_stop
     can terminate a manual /scan (same pattern as scheduled_scan_cron).
t3 — _release_backfill_lock must NOT unlink the lock file
     (unlink-after-unlock race).
t4 — .gitignore lists reconciliation_status.json.
t5 — telegram_media._rewind_upload_files maps ValueError ("seek of closed
     file") onto OSError so _telegram_post converts it into
     requests.RequestException.

Run:
    cd /srv/civitai-monitor
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_item_guards.py -q -p no:cacheprovider
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

import civitai_client
import telegram_media
from civitai_client import FetchPageError

# Load civitai-bot.py as a module (hyphenated name needs importlib) — same
# shim as tests/test_bot.py under a distinct module name so the full-suite
# run never collides with it.
_spec = importlib.util.spec_from_file_location(
    "civitai_bot_item_guards", str(Path(__file__).parent.parent / "civitai-bot.py")
)
civitai_bot = importlib.util.module_from_spec(_spec)
sys.modules["civitai_bot_item_guards"] = civitai_bot
_spec.loader.exec_module(civitai_bot)


# ---------------------------------------------------------------------------
# t1 — fetch_page items type guard
# ---------------------------------------------------------------------------

class TestFetchPageItemsGuard:
    @staticmethod
    def _resp(payload):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        return resp

    def test_items_string_raises_fetch_page_error(self):
        with patch.object(
            civitai_client, "safe_get", return_value=self._resp({"items": "abc"})
        ), pytest.raises(FetchPageError, match="unexpected items type str"):
            civitai_client.fetch_page("alice", nsfw=False)

    def test_items_int_raises_fetch_page_error(self):
        with patch.object(
            civitai_client, "safe_get", return_value=self._resp({"items": 5})
        ), pytest.raises(FetchPageError, match="unexpected items type int"):
            civitai_client.fetch_page("alice", nsfw=False)

    def test_items_none_raises_fetch_page_error(self):
        with patch.object(
            civitai_client, "safe_get", return_value=self._resp({"items": None})
        ), pytest.raises(FetchPageError):
            civitai_client.fetch_page("alice", nsfw=False)

    def test_non_dict_elements_are_filtered(self):
        payload = {"items": [{"id": 1}, 5, "x", None, {"id": 2}], "metadata": {}}
        with patch.object(civitai_client, "safe_get", return_value=self._resp(payload)):
            items, cursor = civitai_client.fetch_page("alice", nsfw=False)
        assert items == [{"id": 1}, {"id": 2}]
        assert cursor == ""

    def test_missing_items_defaults_to_empty_list(self):
        with patch.object(
            civitai_client, "safe_get", return_value=self._resp({"metadata": {}})
        ):
            items, cursor = civitai_client.fetch_page("alice", nsfw=False)
        assert items == []
        assert cursor == ""

    def test_normal_items_no_regression(self):
        payload = {"items": [{"id": 7}], "metadata": {"nextCursor": "c9"}}
        with patch.object(civitai_client, "safe_get", return_value=self._resp(payload)):
            items, cursor = civitai_client.fetch_page("alice", nsfw=False)
        assert items == [{"id": 7}]
        assert cursor == "c9"


# ---------------------------------------------------------------------------
# t2 — /stop must be able to terminate a scan spawned by /scan
# ---------------------------------------------------------------------------

def _make_fake_proc(returncode=None):
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = returncode
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    return proc


def _make_update(user_id=8628596870):
    update = MagicMock()
    update.effective_user.id = user_id
    update.message.reply_text = AsyncMock()
    return update


class TestStopCanTerminateManualScan:
    """cmd_scan must publish its subprocess like scheduled_scan_cron does,
    so cmd_stop (and _kill_running_scan) can terminate a manual /scan."""

    @pytest.mark.asyncio
    async def test_cmd_stop_terminates_scan_spawned_by_cmd_scan(self, monkeypatch):
        proc = _make_fake_proc(returncode=None)
        proc.terminate.side_effect = lambda *a, **k: setattr(proc, "returncode", -15)

        create_subproc = AsyncMock(return_value=proc)
        monkeypatch.setattr(civitai_bot.asyncio, "create_subprocess_exec", create_subproc)
        monkeypatch.setattr(civitai_bot, "_check_auth", AsyncMock(return_value=True))

        registered_during_communicate = {}

        async def fake_communicate(p, timeout=0):
            # Snapshot the registration while the manual scan is running and
            # fire /stop against it, exactly like a user would.
            registered_during_communicate["scan_proc"] = civitai_bot._current_scan_proc
            await civitai_bot.cmd_stop(_make_update(), None)
            return b"", b""

        monkeypatch.setattr(civitai_bot, "communicate_with_idle_timeout", fake_communicate)

        civitai_bot._current_scan_proc = None
        civitai_bot._current_recon_proc = None
        if hasattr(civitai_bot, "_user_last_call"):
            civitai_bot._user_last_call.clear()
        try:
            await civitai_bot.cmd_scan(_make_update(), None)
        finally:
            civitai_bot._current_scan_proc = None
            civitai_bot._current_recon_proc = None

        create_subproc.assert_called_once()
        # cmd_scan published the subprocess for /stop while it was running
        assert registered_during_communicate["scan_proc"] is proc
        # /stop terminated the manual scan
        proc.terminate.assert_called_once()
        # the slot was released once the subprocess was done
        assert civitai_bot._current_scan_proc is None

    @pytest.mark.asyncio
    async def test_cmd_stop_kills_registered_manual_scan_handle(self, monkeypatch):
        """Direct variant: a registered, still-running manual-scan proc gets
        terminate() + wait() from cmd_stop (mirrors test_bot.py's recon test)."""
        proc = _make_fake_proc(returncode=None)
        civitai_bot._current_scan_proc = proc
        civitai_bot._current_recon_proc = None
        monkeypatch.setattr(civitai_bot, "_check_auth", AsyncMock(return_value=True))
        try:
            await civitai_bot.cmd_stop(_make_update(), None)
            proc.terminate.assert_called_once()
            proc.wait.assert_called()
        finally:
            civitai_bot._current_scan_proc = None
            civitai_bot._current_recon_proc = None


# ---------------------------------------------------------------------------
# t3 — backfill lock file must survive release (no unlink-after-unlock)
# ---------------------------------------------------------------------------

class TestBackfillLockFilePersistence:
    def test_lock_file_persists_after_release(self, tmp_path, monkeypatch):
        monkeypatch.setattr(civitai_bot, "SCRIPT_DIR", tmp_path)
        acquired = civitai_bot._acquire_backfill_lock("111", "alice")
        assert acquired is not None
        fd, path = acquired
        try:
            assert path.exists()
            assert path.parent == tmp_path
            civitai_bot._release_backfill_lock(fd, path)
            # The sentinel must survive the release: unlink-after-unlock
            # races with a waiter that has just opened the same path.
            assert path.exists()
        finally:
            if path.exists():
                path.unlink()

    def test_lock_is_really_released_for_flock_semantics(self, tmp_path, monkeypatch):
        monkeypatch.setattr(civitai_bot, "SCRIPT_DIR", tmp_path)
        first = civitai_bot._acquire_backfill_lock("222", "bob")
        assert first is not None
        civitai_bot._release_backfill_lock(*first)
        # flock on the live inode must be free again: immediate re-acquire wins.
        second = civitai_bot._acquire_backfill_lock("222", "bob")
        assert second is not None
        fd, path = second
        civitai_bot._release_backfill_lock(fd, path)

    def test_acquire_fails_while_lock_held(self, tmp_path, monkeypatch):
        monkeypatch.setattr(civitai_bot, "SCRIPT_DIR", tmp_path)
        first = civitai_bot._acquire_backfill_lock("333", "carol")
        assert first is not None
        try:
            assert civitai_bot._acquire_backfill_lock("333", "carol") is None
        finally:
            civitai_bot._release_backfill_lock(*first)


# ---------------------------------------------------------------------------
# t4 — .gitignore lists the runtime status file
# ---------------------------------------------------------------------------

class TestGitignoreRuntimeFiles:
    @staticmethod
    def _gitignore_text():
        return (Path(__file__).parent.parent / ".gitignore").read_text(encoding="utf-8")

    def test_gitignore_lists_reconciliation_status(self):
        assert "reconciliation_status.json" in self._gitignore_text()

    def test_gitignore_lists_backfill_lock_sentinels(self):
        # Lock files are permanent now (t3) — they must not show up untracked.
        assert ".backfill_lock_*.lck" in self._gitignore_text()


# ---------------------------------------------------------------------------
# t5 — _rewind_upload_files seek exception surface (ValueError -> OSError)
# ---------------------------------------------------------------------------

class _ClosedHandle:
    """File-like whose seek() raises ValueError like a closed file object."""

    def seek(self, offset, whence=0):
        raise ValueError("seek of closed file")


class TestRewindUploadFilesValueErrorSurface:
    def test_rewind_maps_valueerror_to_oserror(self):
        with pytest.raises(OSError, match="cannot rewind"):
            telegram_media._rewind_upload_files({"video": _ClosedHandle()})

    def test_rewind_maps_valueerror_in_tuple_form(self):
        with pytest.raises(OSError, match="cannot rewind"):
            telegram_media._rewind_upload_files(
                {"media": ("x.png", _ClosedHandle(), "image/png")}
            )

    def test_telegram_post_wraps_rewind_failure_in_request_exception(self):
        fake_token = "123" + ":" + "A" * 10  # fake fixture token, split parts
        url = f"https://api.telegram.org/bot{fake_token}/sendVideo"
        api = MagicMock()
        with patch.object(telegram_media.requests, "post", api), pytest.raises(
            requests.RequestException, match="upload file rewind failed"
        ):
            telegram_media._telegram_post(url, timeout=1.0, files={"video": _ClosedHandle()})
        api.post.assert_not_called()


# ---------------------------------------------------------------------------
# t6 — the document-send branch is gone for good
# ---------------------------------------------------------------------------


# Assembled at runtime so this test file itself never contains the endpoint
# name as a literal (the guard below scans every file, including this one).
_DOC_ENDPOINT = "send" + "Document"


class TestDocumentSendBranchRemoved:
    """Oversized media is compressed; the Telegram document path is gone.

    The guard scans every shipped source/deployment file (Python, workflows,
    YAML, systemd units, docs) for the endpoint name — the changelog is the
    single exemption, because release history legitimately records that older
    versions used it.
    """

    _SCANNED_SUFFIXES = (".py", ".yml", ".yaml", ".service", ".md")
    _SCANNED_SKIP_NAMES = ("CHANGELOG.md",)

    @staticmethod
    def _scan(root: Path) -> list[str]:
        offenders = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in TestDocumentSendBranchRemoved._SCANNED_SUFFIXES:
                continue
            if path.name in TestDocumentSendBranchRemoved._SCANNED_SKIP_NAMES:
                continue
            if path.name.endswith(".bak"):
                continue
            if any(part in {".venv", ".git", "__pycache__", "downloads"} for part in path.parts):
                continue
            if _DOC_ENDPOINT in path.read_text(encoding="utf-8", errors="replace"):
                offenders.append(str(path.relative_to(root)))
        return offenders

    def test_no_shipped_file_references_the_document_endpoint(self):
        root = Path(__file__).parent.parent
        offenders = self._scan(root)
        assert offenders == [], f"document-send endpoint still referenced by: {offenders}"

    def test_guard_actually_detects_the_endpoint(self, tmp_path):
        # Guard the guard: an offending file must be found, not silently ignored.
        (tmp_path / "bad.py").write_text(
            f"url = 'send?' + '{_DOC_ENDPOINT}'", encoding="utf-8",
        )
        assert self._scan(tmp_path) == ["bad.py"]

    def test_document_limit_constant_is_gone(self):
        removed_constant = "TELEGRAM_" + "DOCUMENT_MAX_BYTES"
        assert not hasattr(telegram_media, removed_constant)

    def test_oversized_media_uses_the_compression_settings(self):
        # The photo limit constant is now genuinely used by the send path.
        settings = telegram_media.media_compress.CompressSettings()
        assert settings.photo_target_bytes == telegram_media.TELEGRAM_PHOTO_MAX_BYTES
        assert settings.video_target_bytes == telegram_media.TELEGRAM_VIDEO_MAX_MB * 1024 * 1024
