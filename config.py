# =============================================================================
# DEPRECATED — This module is no longer used by the main application.
# The unified config lives in monitor.py (MonitorConfig + load_config).
# Keep this file for reference only; do NOT import from it.
# =============================================================================
import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings
from typing import List, Dict, Optional
from pathlib import Path

class TelegramSettings(BaseSettings):
    bot_token: str = Field(..., description="Telegram Bot Token")
    chat_id: str = Field(..., description="Telegram Chat ID (can be group or channel)")

class Settings(BaseSettings):
    # Core
    mode: str = Field("incremental", pattern="^(incremental|full)$")
    nsfw: str = Field("both", pattern="^(sfw_only|nsfw_only|both)$")
    video_enabled: bool = True
    max_video_size_mb: int = Field(1024, ge=1, le=4096)
    download_workers: int = Field(4, ge=1, le=16)
    keep_days: int = Field(30, ge=1)

    # Telegram
    telegram: TelegramSettings

    # Users & Subs
    authorized_users: List[int] = Field(default_factory=list)
    subscriptions: Dict[str, List[str]] = Field(default_factory=dict)  # tg_id -> list of civitai usernames

    # Paths
    data_dir: str = "seen_ids"
    download_dir: str = "downloads"
    cookies_file: Optional[str] = "civitai_cookies.txt"
    log_file: str = "monitor.log"

    # API
    civitai_api_base: str = "https://civitai.com/api/v1"
    page_size: int = 100

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        yaml_file = "config.yaml"
        extra = "ignore"

    @field_validator("download_workers")
    @classmethod
    def clamp_workers(cls, v: int) -> int:
        return max(1, min(16, v))

    @classmethod
    def from_yaml(cls, path: str = "config.yaml") -> "Settings":
        """Load from yaml file, then apply env overrides via pydantic-settings"""
        if not Path(path).exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        
        with open(path, "r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}
        
        # Merge with env (pydantic will handle env)
        return cls(**yaml_data)

    def save_example(self, path: str = "config.yaml.example"):
        """Generate example config"""
        example = self.model_dump(exclude={"telegram": {"bot_token": True}})
        example["telegram"] = {"bot_token": "YOUR_BOT_TOKEN", "chat_id": "YOUR_CHAT_ID"}
        with open(path, "w", encoding="utf-8") as f:
            yaml.dump(example, f, allow_unicode=True, sort_keys=False)
        print(f"Example config saved to {path}")