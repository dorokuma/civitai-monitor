import pytest
from config import Settings
import tempfile
import yaml
from pathlib import Path

def test_config_from_yaml():
    test_data = {
        "mode": "incremental",
        "nsfw": "both",
        "video_enabled": True,
        "max_video_size_mb": 1024,
        "download_workers": 4,
        "telegram": {
            "bot_token": "test_token",
            "chat_id": "-1001234567890"
        },
        "authorized_users": [123456789],
        "subscriptions": {"123456789": ["testuser"]}
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(test_data, f)
        temp_path = f.name
    
    try:
        settings = Settings.from_yaml(temp_path)
        assert settings.mode == "incremental"
        assert settings.download_workers == 4
        assert settings.telegram.bot_token == "test_token"
    finally:
        Path(temp_path).unlink()

def test_download_workers_bounds():
    # Test that pydantic validator clamps workers
    test_data = {
        "mode": "incremental",
        "telegram": {"bot_token": "t", "chat_id": "c"},
        "authorized_users": [],
        "subscriptions": {},
        "download_workers": 20  # should be clamped to 16
    }
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(test_data, f)
        temp_path = f.name
    try:
        settings = Settings.from_yaml(temp_path)
        assert settings.download_workers == 16  # clamped by validator
    finally:
        Path(temp_path).unlink()