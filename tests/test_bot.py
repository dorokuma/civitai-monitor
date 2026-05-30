"""Tests for civitai-bot.py command-parsing and helper logic."""

import importlib.util
import sys
import pytest

# Load civitai-bot.py as a module (hyphenated name needs importlib)
_spec = importlib.util.spec_from_file_location(
    "civitai_bot_module", "/root/civitai-monitor/civitai-bot.py"
)
civitai_bot = importlib.util.module_from_spec(_spec)
sys.modules["civitai_bot_module"] = civitai_bot
_spec.loader.exec_module(civitai_bot)

from civitai_bot_module import (  # noqa: E402
    MonitorConfig,
    _CIVITAI_URL_PREFIXES,
    get_users,
    parse_username_input,
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
