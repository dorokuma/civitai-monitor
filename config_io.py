"""Config models, load/save, and secret redaction for civitai-monitor."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

log = logging.getLogger("civitai-monitor")

# ---------------------------------------------------------------------------
# Pydantic config models
# ---------------------------------------------------------------------------


class HttpConfig(BaseModel):
    user_agent: str = Field(
        default="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    referer: str = Field(default="https://civitai.com")
    extra_headers: dict[str, str] = Field(default_factory=dict)
    cookies_file: str = Field(default="", description="Path to Netscape-format cookies.txt for Civitai auth")


class DownloadConfig(BaseModel):
    output_dir: str = "downloads"
    keep_days: int = 7
    max_total_gb: int = 10          # 最大缓存总大小（GB），0 表示不限制
    size_suffixes: list[str] = Field(
        default=["/width=1024/", "/width=450/", "/width=640/"]
    )


class ApiConfig(BaseModel):
    base_url: str = "https://civitai.com/api/v1"
    images_per_page: int = 100


class TelegramConfig(BaseModel):
    bot_token: str
    chat_id: str
    api_base_url: str = "https://api.telegram.org"


class DataConfig(BaseModel):
    data_dir: str = ""
    seen_ids_file: str = "seen_ids.json"


class IncrementalConfig(BaseModel):
    """Limits that apply to incremental mode (the cron-scheduled scan).

    Incremental is designed to surface *new* content, not to walk an entire
    creator's history. ``max_pages`` caps how many pages we fetch per track
    (SFW/NSFW) on any given scan, so a creator with 1k+ items does not push
    the whole history on the very first run. To backfill the full history,
    use the ``/backfill <user>`` command (which runs ``--mode full``).
    """
    max_pages: int = 5


class BackfillConfig(BaseModel):
    """Configuration for full backfill mode."""
    stale_backfill_minutes: int = Field(
        default=120,
        description="Minutes after which an active backfill is considered stale/zombie"
    )


class MonitorConfig(BaseModel):
    users: list = Field(default_factory=list)
    subscriptions: dict[str, list] = Field(default_factory=dict)
    authorized_users: list[int] = Field(default_factory=list)
    mode: Literal["incremental", "full"] = "incremental"
    nsfw: Literal["sfw_only", "nsfw_only", "both"] = "both"
    api: ApiConfig = Field(default_factory=ApiConfig)
    download: DownloadConfig = Field(default_factory=DownloadConfig)
    telegram: TelegramConfig
    data: DataConfig = Field(default_factory=DataConfig)
    incremental: IncrementalConfig = Field(default_factory=IncrementalConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    backfill: BackfillConfig = Field(default_factory=BackfillConfig)
    video_enabled: bool = True
    max_video_size_mb: int = 1024


# ---------------------------------------------------------------------------
# Paths / constants
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()

DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path.home() / ".civitai-monitor" / "config.yaml",
    SCRIPT_DIR / "config.yaml",
]

VALID_NSFW = {"sfw_only", "nsfw_only", "both"}
VALID_MODES = {"incremental", "full"}

DATA_DIR_NAME = "seen_ids"
STATUS_FILE_NAME = "monitor_status.json"
LOCK_FILE_NAME = ".monitor.lock"


# ---------------------------------------------------------------------------
# Secret redaction + atomic write
# ---------------------------------------------------------------------------


def redact_config_for_disk(data: dict) -> dict:
    """Return a deep-ish copy of config dict with secrets stripped for disk.

    ``telegram.bot_token`` must never be written back: env
    ``CIVITAI_BOT_TOKEN`` is the source of truth and must not leak into yaml.
    """
    out = dict(data)
    tg = out.get("telegram")
    if isinstance(tg, dict):
        tg_copy = dict(tg)
        # Empty string keeps the key present for schema readers; never persist real token.
        tg_copy["bot_token"] = ""
        out["telegram"] = tg_copy
    return out


def write_config(cfg: MonitorConfig, path: Path | None = None) -> None:
    """Atomically write config yaml with secrets redacted.

    Uses tmp file + ``os.replace``. Optional file lock when filelock is available.
    """
    target = Path(path) if path is not None else SCRIPT_DIR / "config.yaml"
    data = redact_config_for_disk(cfg.model_dump(exclude_none=True))

    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_suffix(target.suffix + ".lock")

    def _do_write() -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=target.name + ".",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(
                    data, f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                    indent=2,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    try:
        from filelock import FileLock, Timeout
        try:
            with FileLock(str(lock_path), timeout=10):
                _do_write()
        except Timeout:
            log.warning("Timeout acquiring config write lock for %s; writing unlocked", target)
            _do_write()
    except ImportError:
        _do_write()


def load_config(path: Path | None = None) -> MonitorConfig | None:
    """Load and validate config. Injects ``CIVITAI_BOT_TOKEN`` from env when set."""
    # Late import avoids circular import at module load (civitai_client → config types).
    from civitai_client import init_session

    paths = [Path(path)] if path else DEFAULT_CONFIG_PATHS
    for p in paths:
        if p.exists():
            log.info("Loading config from %s", p)
            with open(p, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            # Environment variable overrides for sensitive fields
            if os.environ.get("CIVITAI_BOT_TOKEN"):
                raw.setdefault("telegram", {})["bot_token"] = os.environ["CIVITAI_BOT_TOKEN"]
            try:
                cfg = MonitorConfig(**raw)
                init_session(cfg.http)
                return cfg
            except ValidationError as e:
                log.error("Config validation failed: %s", e)
                return None
    log.error("config.yaml not found (searched: %s)", [str(p) for p in paths])
    return None
