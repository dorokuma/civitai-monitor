"""Targeted tests: Retry-After sanitizing (civitai_client) + token-safe logs (telegram_media).

Run:
    cd /srv/civitai-monitor
    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/test_client_retry.py -q -p no:cacheprovider
"""

from __future__ import annotations

import time
from email.utils import formatdate

import requests

import telegram_media
from civitai_client import (
    DEFAULT_RETRY_AFTER_SECONDS,
    MAX_RATE_LIMIT_WAIT_SECONDS,
    RateLimitError,
    _parse_retry_after,
    _rate_limit_wait,
)
from telegram_media import _sanitize_exc


class _FakeResponse:
    """Duck-typed stand-in for requests.Response (RateLimitError only uses .headers)."""

    def __init__(self, headers):
        self.headers = headers
        self.status_code = 429


def _rate_limit_error(retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return RateLimitError(_FakeResponse(headers))


class _FakeOutcome:
    def __init__(self, exc):
        self._exc = exc

    def exception(self):
        return self._exc


class _FakeRetryState:
    """Duck-typed stand-in for tenacity's RetryCallState."""

    def __init__(self, exc=None, attempt_number=1):
        self.outcome = _FakeOutcome(exc)
        self.attempt_number = attempt_number


# ---------------------------------------------------------------------------
# t1-t2: Retry-After header sanitizing (HTTP-date / garbage must not crash)
# ---------------------------------------------------------------------------

def test_retry_after_http_date_is_parsed_not_crash():
    """An HTTP-date Retry-After is legal; int("Wed, ...") used to raise ValueError."""
    future = int(time.time()) + 120
    exc = _rate_limit_error(formatdate(future, usegmt=True))
    assert 110 <= exc.retry_after <= 121


def test_retry_after_http_date_in_past_falls_back_to_default():
    """A date already in the past parses but yields seconds <= 0 -> default."""
    exc = _rate_limit_error("Wed, 21 Oct 2015 07:28:00 GMT")
    assert exc.retry_after == DEFAULT_RETRY_AFTER_SECONDS == 30


def test_retry_after_garbage_falls_back_to_default():
    """t2: a non-numeric, non-date value falls back to 30 instead of raising."""
    exc = _rate_limit_error("abc")
    assert exc.retry_after == 30


def test_retry_after_zero_or_negative_falls_back_to_default():
    assert _rate_limit_error("0").retry_after == 30
    assert _rate_limit_error("-5").retry_after == 30


def test_retry_after_missing_header_falls_back_to_default():
    assert _rate_limit_error(None).retry_after == 30


def test_parse_retry_after_direct():
    assert _parse_retry_after("7") == 7
    assert _parse_retry_after("  7  ") == 7
    assert _parse_retry_after(None) == 30
    assert _parse_retry_after("") == 30
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 30


# ---------------------------------------------------------------------------
# t3-t4: _rate_limit_wait honors and clamps Retry-After
# ---------------------------------------------------------------------------

def test_rate_limit_wait_huge_value_clamped_to_cap():
    """t3: a huge Retry-After must not hang the monitor while it holds .monitor.lock."""
    state = _FakeRetryState(_rate_limit_error("999999999"))
    wait = _rate_limit_wait(state)
    assert wait == MAX_RATE_LIMIT_WAIT_SECONDS == 120
    assert wait <= MAX_RATE_LIMIT_WAIT_SECONDS


def test_rate_limit_wait_slightly_over_cap_clamped_too():
    state = _FakeRetryState(_rate_limit_error("300"))
    assert _rate_limit_wait(state) == MAX_RATE_LIMIT_WAIT_SECONDS


def test_rate_limit_wait_normal_value_plus_jitter():
    """t4: normal value is honored (plus the 0..2s jitter), not clamped."""
    state = _FakeRetryState(_rate_limit_error("7"))
    wait = _rate_limit_wait(state)
    assert 7.0 <= wait <= 9.0


def test_rate_limit_wait_non_rate_limit_uses_exponential_backoff():
    state = _FakeRetryState(requests.ConnectionError("boom"), attempt_number=3)
    wait = _rate_limit_wait(state)
    assert 2 <= wait <= 30


# ---------------------------------------------------------------------------
# t5: _sanitize_exc masks the bot token, leaves normal text alone
# ---------------------------------------------------------------------------

def test_sanitize_exc_masks_bot_token_in_url():
    """t5: the /bot<token> URL segment inside a requests exception must be masked."""
    token = "123456" + ":" + "A" * 20  # fake token assembled from parts
    exc = requests.RequestException(
        "HTTPSConnectionPool(host='api.telegram.org', port=443): "
        f"Max retries exceeded with url: /bot{token}/sendMessage (Caused by ...)"
    )
    sanitized = _sanitize_exc(exc)
    assert token not in sanitized
    assert "/bot***" in sanitized
    assert "/sendMessage" in sanitized


def test_sanitize_text_masks_token_with_custom_api_base_path():
    token = "999" + ":" + "b" * 15
    text = f"https://tg.example.com/bot{token}/sendVideo failed"
    sanitized = telegram_media._sanitize_text(text)
    assert token not in sanitized
    assert sanitized == "https://tg.example.com/bot***/sendVideo failed"


def test_sanitize_exc_leaves_normal_urls_alone():
    """t5: URLs without a /bot<token> segment are not damaged."""
    msg = "error while fetching https://civitai.com/api/v1/images?username=x&limit=100"
    sanitized = _sanitize_exc(requests.ConnectionError(msg))
    assert sanitized == msg


def test_sanitize_exc_plain_message_untouched():
    assert _sanitize_exc(ValueError("chat not found")) == "chat not found"


def test_sanitize_text_replaces_every_occurrence():
    token = "1" + ":" + "c" * 10
    text = f"/bot{token}/a then /bot{token}/b"
    assert telegram_media._sanitize_text(text) == "/bot***/a then /bot***/b"
