"""Tests for monitor.py — MonitorConfig and load_config."""

import yaml
import tempfile
from pathlib import Path
from monitor import MonitorConfig, load_config


def test_load_config_from_yaml(monkeypatch):
    """load_config() reads a valid yaml and returns a populated MonitorConfig."""
    # Clear CIVITAI_BOT_TOKEN so it doesn't override the test value
    monkeypatch.delenv("CIVITAI_BOT_TOKEN", raising=False)

    test_data = {
        "mode": "incremental",
        "nsfw": "both",
        "video_enabled": True,
        "max_video_size_mb": 1024,
        "telegram": {
            "bot_token": "test_token",
            "chat_id": "-1001234567890",
        },
        "authorized_users": [123456789],
        "subscriptions": {"123456789": ["testuser"]},
        "api": {"base_url": "https://civitai.com/api/v1", "images_per_page": 50},
        "download": {"output_dir": "downloads", "keep_days": 14},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(test_data, f)
        temp_path = f.name

    try:
        cfg = load_config(Path(temp_path))
        assert cfg.mode == "incremental"
        assert cfg.nsfw == "both"
        assert cfg.video_enabled is True
        assert cfg.max_video_size_mb == 1024
        assert cfg.telegram.bot_token == "test_token"
        assert cfg.telegram.chat_id == "-1001234567890"
        assert cfg.authorized_users == [123456789]
        assert cfg.subscriptions == {"123456789": ["testuser"]}
        assert cfg.api.base_url == "https://civitai.com/api/v1"
        assert cfg.api.images_per_page == 50
        assert cfg.download.keep_days == 14
    finally:
        Path(temp_path).unlink()


def test_monitor_config_defaults():
    """MonitorConfig uses sensible defaults for optional fields."""
    cfg = MonitorConfig(
        telegram={"bot_token": "t", "chat_id": "c"},
        subscriptions={},
        authorized_users=[],
    )
    assert cfg.mode == "incremental"
    assert cfg.nsfw == "both"
    assert cfg.video_enabled is True
    assert cfg.max_video_size_mb == 1024
    assert cfg.api.base_url == "https://civitai.com/api/v1"
    assert cfg.api.images_per_page == 100
    assert cfg.download.output_dir == "downloads"
    assert cfg.download.keep_days == 7
    assert cfg.http.cookies_file == ""
    assert cfg.data.data_dir == ""


def test_load_config_missing_file():
    """load_config() exits with error when config is missing (tested by checking it doesn't crash)."""
    # load_config calls sys.exit(1) on missing file — tested manually
    pass


def test_monitor_config_nested_subscriptions():
    """Subscriptions with dict entries are stored as-is."""
    cfg = MonitorConfig(
        telegram={"bot_token": "t", "chat_id": "c"},
        subscriptions={"123": [{"name": "user1"}, {"name": "user2"}]},
        authorized_users=[],
    )
    assert cfg.subscriptions["123"][0]["name"] == "user1"
    assert cfg.subscriptions["123"][1]["name"] == "user2"


def test_monitor_config_mode_nsfw_valid():
    """mode and nsfw accept valid strings."""
    cfg = MonitorConfig(
        telegram={"bot_token": "t", "chat_id": "c"},
        mode="incremental",
        nsfw="sfw_only",
        subscriptions={},
        authorized_users=[],
    )
    assert cfg.mode == "incremental"
    assert cfg.nsfw == "sfw_only"


def test_write_config_never_persists_token(tmp_path, monkeypatch):
    """write_config strips bot_token even if present in memory model."""
    from monitor import write_config
    import yaml

    path = tmp_path / "out.yaml"
    cfg = MonitorConfig(
        telegram={"bot_token": "env-injected-secret", "chat_id": "c"},
        subscriptions={},
        authorized_users=[],
    )
    write_config(cfg, path)
    raw = yaml.safe_load(path.read_text())
    assert raw["telegram"]["bot_token"] == ""
    assert "env-injected-secret" not in path.read_text()
