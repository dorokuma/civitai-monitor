"""Targeted regression tests: telegram media retry/detailed + cron/config guards.

t1  _telegram_post must rewind upload file objects before every attempt so a
    429/5xx retry re-uploads the full payload instead of 0 bytes (which the
    API answers with 400, permanently losing the media to the text fallback).
t2  send_to_telegram_detailed (delivered, media_failed) contract + the legacy
    send_to_telegram wrapper.
t3  civitai_client.fetch_page guards non-dict JSON payloads (FetchPageError
    instead of AttributeError) and tolerates a non-dict metadata.
t4  scheduled_reconciliation_cron falls back to 03:30 on invalid HH:MM and
    the coroutine survives (no ValueError from datetime.replace).
t5  config_io.load_config returns None on yaml.YAMLError (half-written file).
t6  civitai-bot._load_interval falls back to 600 on scalar/array top level or
    non-int seconds.
"""

import importlib.util
import io
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

_REPO_ROOT = Path(__file__).parent.parent

# Load civitai-bot.py as a module (hyphenated name needs importlib) —
# same approach as tests/test_bot.py, under a private module name.
_spec = importlib.util.spec_from_file_location(
    "civitai_bot_media_cron", str(_REPO_ROOT / "civitai-bot.py")
)
civitai_bot = importlib.util.module_from_spec(_spec)
sys.modules["civitai_bot_media_cron"] = civitai_bot
_spec.loader.exec_module(civitai_bot)

import civitai_client
import config_io
import telegram_media

# Fixture token, split per secret hygiene rules (never a real token).
FAKE_TOKEN = "123" + ":AA" + "BB"


def _api_response(status_code=200, ok=True, headers=None, text="{}"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = ok
    resp.headers = headers or {}
    resp.text = text
    return resp


class _FakeTelegramAPI:
    """Stands in for requests.post, routed by Telegram endpoint name."""

    def __init__(self):
        self.media_responses = []
        self.text_response = _api_response(200, True)
        self.media_exception = None
        self.text_calls = 0

    def __call__(self, url, timeout=None, **kwargs):
        if "sendMessage" in url:
            self.text_calls += 1
            return self.text_response
        if self.media_exception is not None:
            raise self.media_exception
        return self.media_responses.pop(0)


# ---------------------------------------------------------------------------
# t1 — _telegram_post rewind-on-retry
# ---------------------------------------------------------------------------


class TestTelegramPostRewind:
    def test_retry_after_429_reuploads_full_payload(self):
        content = b"PNGDATA-" * 250  # 2000 bytes of "media"
        responses = [
            _api_response(429, False, headers={"Retry-After": "0"}),
            _api_response(200, True),
        ]
        bodies = []

        def fake_post(url, timeout=None, **kwargs):
            fileobj = kwargs["files"]["video"][1]
            bodies.append(fileobj.read())  # requests consumes the stream
            return responses.pop(0)

        with patch.object(telegram_media.requests, "post", side_effect=fake_post), \
             patch.object(telegram_media.time, "sleep", lambda *_a, **_k: None):
            resp = telegram_media._telegram_post(
                "https://api.telegram.test/bot" + FAKE_TOKEN + "/sendVideo",
                timeout=1,
                data={"chat_id": "42"},
                files={"video": ("clip.mp4", io.BytesIO(content), "video/mp4")},
            )

        assert resp.ok is True
        assert bodies == [content, content]  # 2nd attempt must not be 0 bytes


# ---------------------------------------------------------------------------
# t2 — send_to_telegram_detailed contract
# ---------------------------------------------------------------------------


class TestSendToTelegramDetailed:
    @staticmethod
    def _image(tmp_path):
        img = tmp_path / "pic.png"
        img.write_bytes(b"\x89PNG" + b"0" * 64)
        return img

    def test_media_429_exhausted_falls_back_text(self, tmp_path):
        img = self._image(tmp_path)
        api = _FakeTelegramAPI()
        api.media_responses = [
            _api_response(429, False, headers={"Retry-After": "0"})
            for _ in range(telegram_media._TG_MAX_RETRIES)
        ]
        with patch.object(telegram_media.requests, "post", api), \
             patch.object(telegram_media.time, "sleep", lambda *_a, **_k: None):
            assert telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "new art", [img]
            ) == (True, True)
        assert api.text_calls == 1

    def test_media_success(self, tmp_path):
        img = self._image(tmp_path)
        api = _FakeTelegramAPI()
        api.media_responses = [_api_response(200, True)]
        with patch.object(telegram_media.requests, "post", api), \
             patch.object(telegram_media.time, "sleep", lambda *_a, **_k: None):
            assert telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "new art", [img]
            ) == (True, False)
        assert api.text_calls == 0

    def test_pure_text_media_failed_always_false(self):
        api = _FakeTelegramAPI()
        with patch.object(telegram_media.requests, "post", api), \
             patch.object(telegram_media.time, "sleep", lambda *_a, **_k: None):
            assert telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "hello", None
            ) == (True, False)
        assert api.text_calls == 1

    def test_missing_files_means_pure_text(self, tmp_path):
        api = _FakeTelegramAPI()
        with patch.object(telegram_media.requests, "post", api), \
             patch.object(telegram_media.time, "sleep", lambda *_a, **_k: None):
            ok, media_failed = telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "hello", [tmp_path / "ghost.png"]
            )
        assert (ok, media_failed) == (True, False)
        assert api.text_calls == 1

    def test_video_400_text_fallback_delivered(self, tmp_path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"VID" * 32)
        api = _FakeTelegramAPI()
        api.media_responses = [_api_response(400, False, text="Bad Request: wrong file")]
        with patch.object(telegram_media.requests, "post", api), \
             patch.object(telegram_media.time, "sleep", lambda *_a, **_k: None):
            assert telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [video]
            ) == (True, True)

    def test_video_timeout_uncertain_no_text_fallback(self, tmp_path):
        """Legacy shim keeps returning None; detailed reports (False, True)."""
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"VID" * 32)
        api = _FakeTelegramAPI()
        api.media_exception = requests.Timeout("read timed out")
        with patch.object(telegram_media.requests, "post", api), \
             patch.object(telegram_media.time, "sleep", lambda *_a, **_k: None):
            assert telegram_media._send_telegram_video(
                "http://api/bot" + FAKE_TOKEN, "42", "caption", video
            ) is None
            assert telegram_media.send_to_telegram_detailed(
                FAKE_TOKEN, "42", "caption", [video]
            ) == (False, True)
        assert api.text_calls == 0  # anti-duplicate: no text after timeout

    def test_wrapper_returns_delivered(self, tmp_path):
        img = self._image(tmp_path)
        api = _FakeTelegramAPI()
        api.media_responses = [_api_response(200, True)]
        with patch.object(telegram_media.requests, "post", api), \
             patch.object(telegram_media.time, "sleep", lambda *_a, **_k: None):
            assert telegram_media.send_to_telegram(
                FAKE_TOKEN, "42", "new art", [img]
            ) is True


# ---------------------------------------------------------------------------
# t3 — fetch_page JSON payload guards
# ---------------------------------------------------------------------------


class TestFetchPageJsonGuards:
    @staticmethod
    def _resp(payload):
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = payload
        return resp

    def test_list_payload_raises_fetch_page_error(self):
        with patch.object(
            civitai_client, "safe_get", return_value=self._resp(["bad", "shape"])
        ), pytest.raises(
            civitai_client.FetchPageError, match="unexpected JSON payload type list"
        ):
            civitai_client.fetch_page("alice", nsfw=False)

    def test_scalar_payload_raises_fetch_page_error(self):
        with patch.object(
            civitai_client, "safe_get", return_value=self._resp("oops")
        ), pytest.raises(civitai_client.FetchPageError):
            civitai_client.fetch_page("alice", nsfw=False)

    def test_dict_payload_normal(self):
        payload = {"items": [{"id": 7}], "metadata": {"nextCursor": "c9"}}
        with patch.object(civitai_client, "safe_get", return_value=self._resp(payload)):
            items, cursor = civitai_client.fetch_page("alice", nsfw=False)
        assert items == [{"id": 7}]
        assert cursor == "c9"

    def test_non_dict_metadata_does_not_crash(self):
        payload = {"items": [{"id": 8}], "metadata": "not-a-dict"}
        with patch.object(civitai_client, "safe_get", return_value=self._resp(payload)):
            items, cursor = civitai_client.fetch_page("alice", nsfw=False)
        assert items == [{"id": 8}]
        assert cursor == ""


# ---------------------------------------------------------------------------
# t4 — scheduled_reconciliation_cron invalid HH:MM guard
# ---------------------------------------------------------------------------


class TestReconciliationCronTimeGuard:
    @staticmethod
    def _cfg(time_str):
        return civitai_bot.MonitorConfig(
            telegram={"bot_token": FAKE_TOKEN, "chat_id": "42"},
            reconciliation={"enabled": True, "time": time_str},
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_time", ["25:00", "03:60"])
    async def test_invalid_time_falls_back_and_cron_survives(self, monkeypatch, caplog, bad_time):
        monkeypatch.setattr(civitai_bot, "read_config", lambda: self._cfg(bad_time))
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        monkeypatch.setattr(
            civitai_bot, "_load_reconciliation_last_success", lambda *_a, **_k: today
        )

        sleep_mock = AsyncMock()

        def _stop_loop(*_a, **_k):
            civitai_bot._shutdown_requested = True

        sleep_mock.side_effect = _stop_loop

        with caplog.at_level(logging.WARNING), \
             patch.object(civitai_bot.asyncio, "sleep", sleep_mock):
            civitai_bot._shutdown_requested = False
            try:
                await civitai_bot.scheduled_reconciliation_cron()
            finally:
                civitai_bot._shutdown_requested = False

        fallback_warnings = [
            rec for rec in caplog.records
            if "Invalid reconciliation time" in rec.getMessage()
        ]
        assert fallback_warnings, f"expected 03:30 fallback warning for {bad_time!r}"
        # Reaching this point proves the coroutine survived: no ValueError
        # escaped from now.replace(hour=25) / replace(minute=60).


# ---------------------------------------------------------------------------
# t5 — config_io.load_config yaml.YAMLError guard
# ---------------------------------------------------------------------------


class TestLoadConfigYamlError:
    def test_broken_yaml_returns_none(self, tmp_path):
        bad = tmp_path / "config.yaml"
        bad.write_text("telegram: [unclosed\n", encoding="utf-8")
        assert config_io.load_config(bad) is None

    def test_valid_config_still_loads(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CIVITAI_BOT_TOKEN", raising=False)
        good = tmp_path / "config.yaml"
        good.write_text(
            "users: []\n"
            "telegram:\n"
            '  bot_token: "' + FAKE_TOKEN + '"\n'
            '  chat_id: "42"\n',
            encoding="utf-8",
        )
        cfg = config_io.load_config(good)
        assert cfg is not None
        assert cfg.telegram.bot_token == FAKE_TOKEN
        assert cfg.telegram.chat_id == "42"


# ---------------------------------------------------------------------------
# t6 — civitai-bot._load_interval type hardening
# ---------------------------------------------------------------------------


class TestLoadInterval:
    @staticmethod
    def _config(tmp_path, payload):
        path = tmp_path / "interval.json"
        path.write_text(payload, encoding="utf-8")
        return path

    def test_normal_dict_returns_seconds(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            civitai_bot, "INTERVAL_CONFIG", self._config(tmp_path, '{"seconds": 300}')
        )
        assert civitai_bot._load_interval() == 300

    def test_top_level_list_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            civitai_bot, "INTERVAL_CONFIG", self._config(tmp_path, "[600, 300]")
        )
        assert civitai_bot._load_interval() == 600

    def test_top_level_string_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            civitai_bot, "INTERVAL_CONFIG", self._config(tmp_path, json.dumps("seconds"))
        )
        assert civitai_bot._load_interval() == 600

    def test_non_int_seconds_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            civitai_bot, "INTERVAL_CONFIG", self._config(tmp_path, '{"seconds": "300"}')
        )
        assert civitai_bot._load_interval() == 600

    def test_missing_file_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setattr(civitai_bot, "INTERVAL_CONFIG", tmp_path / "missing.json")
        assert civitai_bot._load_interval() == 600


class TestLastScanErrorLine:
    def test_extracts_last_page_failure_reason(self, tmp_path):
        log = tmp_path / "bot.log"
        log.write_text(
            "2026-09-13 00:00:14 +0000 [INFO] civitai-monitor: Scan starting\n"
            "2026-09-13 00:00:20 +0000 [WARNING] civitai-monitor: Page query failed (track=SFW): "
            "503 Server Error: Service Unavailable for url: https://civitai.com/api/v1/images?username=x\n"
            "2026-09-13 00:01:00 +0000 [INFO] civitai-bot: Scheduled scan starting...\n"
        )
        reason = civitai_bot._last_scan_error_line(str(log))
        assert reason.startswith("Page query failed (track=SFW): 503")
        assert len(reason) <= 200

    def test_last_occurrence_wins(self, tmp_path):
        log = tmp_path / "bot.log"
        log.write_text(
            "x civitai-monitor: Page query failed (track=NSFW): 503 first\n"
            "y civitai-monitor: Fatal page fetch failure: 503 last\n"
        )
        assert "Fatal page fetch failure: 503 last" in civitai_bot._last_scan_error_line(str(log))

    def test_no_error_lines_returns_empty(self, tmp_path):
        log = tmp_path / "bot.log"
        log.write_text("all good\n")
        assert civitai_bot._last_scan_error_line(str(log)) == ""

    def test_missing_file_returns_empty(self, tmp_path):
        assert civitai_bot._last_scan_error_line(str(tmp_path / "nope.log")) == ""
