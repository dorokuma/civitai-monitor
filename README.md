<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.svg">
  <img alt="civitai-monitor logo" src="assets/logo-light.svg" width="64" height="64">
</picture>

# civitai-monitor

**Follow Civitai creators in Telegram — automatically downloads full-resolution originals &amp; HD videos, compresses oversized media without losing posts, and runs smoothly on 1 GB VPS.**

![Python](https://img.shields.io/badge/Python-3.10%2B-18181b?style=flat-square&logo=python&logoColor=white)
![Telegram Bot](https://img.shields.io/badge/Telegram-Bot-18181b?style=flat-square&logo=telegram&logoColor=white)
![1 GB VPS](https://img.shields.io/badge/1%20GB-VPS%20Friendly-18181b?style=flat-square&logo=linux&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-18181b?style=flat-square&logo=opensourceinitiative&logoColor=white)

[Quick Start](#quick-start) · [Highlights](#highlights) · [Bot Commands](#bot-commands) · [Configuration](#more-details)

**English** · [简体中文](README.zh-CN.md)

</div>

```mermaid
flowchart LR
    A["Civitai Creators"] --> B["Built-in Scheduler / Bot Commands"]
    B --> C["Full-Res Original &amp; HD Video Downloader"]
    C --> D["Smart Lossless-First Compressor<br/>(>10 MiB img / >2 GiB vid)"]
    D --> E["Telegram Channel"]
```

<a id="highlights"></a>

## Highlights

| | |
|---|---|
| 📸 **Lossless originals &amp; HD video** | Auto-rewrites URLs to fetch full-resolution images and real HD video from the Backblaze B2 CDN — not just cover thumbnails. |
| 🤖 **Telegram bot with buttons** | Subscribe / unsubscribe / backfill / manual scan / set interval, all via paged interactive buttons. Each Telegram user keeps an isolated subscription list. |
| 🗜 **Smart compression, no dropped posts** | Images over 10 MiB and videos over 2 GiB go through a lossless-first compression ladder with a final fallback — posts always reach the channel. |
| 🪶 **1 GB VPS friendly** | Static memory bounds via systemd (e.g. `MemoryMax`), streaming/lightweight processing, OOM-safe resumable backfill, and `fcntl` file locks that prevent duplicate pushes. |

<a id="quick-start"></a>

## Quick Start

### 1. Install

```bash
git clone https://github.com/dorokuma/civitai-monitor.git
cd civitai-monitor
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# Video compression needs ffmpeg/ffprobe on PATH, e.g.:
sudo apt-get install -y ffmpeg
```

### 2. Configure `.env`

```bash
cp config.yaml.example config.yaml

mkdir -p ~/.civitai-monitor
cat > ~/.civitai-monitor/.env <<'ENV'   # loaded automatically by load_env.py
CIVITAI_BOT_TOKEN=<your-bot-token>
ENV
chmod 600 ~/.civitai-monitor/.env

# edit config.yaml → fill in chat_id, authorized_users, subscriptions
```

Minimal `config.yaml`:

```yaml
telegram:
  chat_id: "<your-telegram-chat-id>"

authorized_users:
  - <your-telegram-user-id>

subscriptions:
  '<your-telegram-user-id>':
    - name: "<target-civitai-username>"
```

> [!NOTE]
> `subscriptions` are keyed by **Telegram user ID**, not the Civitai username.
> Message @userinfobot on Telegram to find your numeric user ID.

### 3. Start Bot &amp; Built-in Scheduler

```bash
.venv/bin/python3 civitai-bot.py
```

> [!NOTE]
> The bot's **built-in scheduler** replaces the old cron job — it runs
> `monitor.py` incremental scans on a configurable interval (default 10 min,
> change it with `/interval`). No crontab setup is needed.

<a id="bot-commands"></a>

## Bot Commands

| Command | What it does |
|---------|--------------|
| `/add <name\|url\|@name>` | Watch a creator — accepts plain names, `@name`, or `civitai.com/user/…` links, and validates the user exists first |
| `/remove` | Paged button list to stop watching a user |
| `/list` | Show your watch list |
| `/status` | Monitor status: disk usage, seen count, config |
| `/scan` | Trigger an incremental scan right now |
| `/backfill` | Paged button list to backfill a creator's full gallery |
| `/interval <min>` | Set the scan interval in minutes (default 10, range 1–1440) |
| `/mode <incremental\|full>` | Switch scan mode (applies globally) |
| `/nsfw <sfw_only\|nsfw_only\|both>` | Switch the NSFW filter (applies globally) |
| `/cleanup [days]` | Prune cached files older than N days |
| `/stop` | Terminate the current scheduled scan and release `.monitor.lock` (e.g. to start a backfill right away) |
| `/help` | Show all commands |

Each authorized Telegram user sees and manages **their own** subscriptions;
media is always pushed to the single `telegram.chat_id` channel.

## Minimum Requirements

| Item | Requirement |
|------|-------------|
| **RAM (min)** | 1 GB — a full backfill can peak near 1.7 GB, so 1 GB works but watch for OOM |
| **RAM (recommended)** | 2 GB or more |
| **Memory bound** | No in-process memory cap — constrain with systemd (e.g. `MemoryMax`) when needed; low-RAM operation relies on streaming/lightweight processing |
| **Disk** | Depends on subscription count and cache policy (`keep_days` + `max_total_gb`); 10 GB+ recommended |
| **Python** | 3.10+ |
| **External tools** | `ffmpeg`/`ffprobe` for video compression |

> [!TIP]
> OOM during a full backfill is not fatal — the resumable backfill mechanism
> automatically picks up where the process was killed.

<a id="more-details"></a>

## More Details

<details>
<summary><b>Environment Variables Reference</b></summary>

| Variable | Required | Purpose |
|----------|----------|---------|
| `CIVITAI_BOT_TOKEN` | ✅ | Telegram bot token. Preferred over `config.yaml` — `write_config` strips `bot_token` when saving yaml. |
| `telegram.chat_id` (yaml) | ✅ | Channel/group/user chat ID that receives pushed media. Single push target for all subscribers. |
| `authorized_users` (yaml) | ✅ | Telegram user IDs allowed to send the bot commands. |
| `subscriptions` (yaml) | ✅ | Per-Telegram-user watch lists, keyed by Telegram user ID. |
| `mode` (yaml) | | `incremental` (default) or `full` backfill. |
| `nsfw` (yaml) | | `sfw_only` / `nsfw_only` / `both` (default). |
| `video_enabled` / `max_video_size_mb` (yaml) | | Toggle video downloads; skip videos larger than N MiB (default 1024). |
| `compress` (yaml) | | Lossless-first compression ladder for oversized media (enabled by default; disabling degrades oversized media to a text notice). |
| `http.cookies_file` (yaml) | video only | Netscape-format cookies.txt exported from a logged-in Civitai browser session — required for real video downloads. |
| `download.keep_days` / `download.max_total_gb` (yaml) | | Cache retention: prune files older than N days or the oldest files once total size exceeds the cap (default 10 GB). |
| `data.data_dir` (yaml) | | Where per-user `seen_ids/` progress JSON lives (empty = repo dir). |
| `incremental.*` / `reconciliation.*` (yaml) | | Page-window and daily 03:30 reconciliation tuning — see `config.yaml.example`. |
| `interval.json` | | Runtime scan interval written by `/interval` (`{"seconds": 600}`). |

See [`config.yaml.example`](config.yaml.example) for the full annotated schema.
`.env` is auto-loaded from `~/.civitai-monitor/.env` (see `load_env.py`).

**Video cookies:** the CDN needs an authenticated session for real video files.
Export `cookies.txt` with a browser extension (e.g. *Get cookies.txt*), point
`http.cookies_file` at it, and re-export when it expires — without it, video
downloads fall back to cover images or text links.

</details>

<details>
<summary><b>systemd Deployment &amp; Generic Backup Guide</b></summary>

Install at `<install-dir>` (e.g. `/opt/civitai-monitor`) and run as `<your-user>`:

```ini
# /etc/systemd/system/civitai-bot.service
[Unit]
Description=Civitai Monitor Bot
After=network.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=<install-dir>
ExecStart=<install-dir>/.venv/bin/python3 <install-dir>/civitai-bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now civitai-bot
```

> [!WARNING]
> **Single-instance constraint.** Mutual exclusion relies on a local `fcntl`
> lock (`.monitor.lock`), so the bot must run on **one host as one instance**
> only. Multi-host or multi-instance deployments invalidate the lock and cause
> duplicate downloads and pushes.

**Backup guide (generic):** keep these small files somewhere safe on your own
backup schedule:

- `<install-dir>/config.yaml`, `<install-dir>/interval.json`, `<install-dir>/active_backfills.json`
- `<install-dir>/seen_ids/` — per-user progress
- `~/.civitai-monitor/.env` and `civitai_cookies.txt` — secrets kept outside the repo

You can skip `downloads/` (re-downloadable cache), `.venv/` (rebuild from
`requirements.lock`), `.git/`, and tool caches. To restore: copy the files back,
rebuild the venv, reinstall dependencies, fix ownership, and restart the service.

</details>

<details>
<summary><b>Project Layout &amp; Local Testing</b></summary>

```
civitai-monitor/
├── monitor.py                  # scanner CLI entrypoint & backward-compat facade
├── civitai-bot.py              # admin bot CLI entrypoint, built-in scheduler & facade
├── civitai_monitor/            # modular scanner, media download, bot state/cron/command package
├── bot_ui.py                   # bot UI helpers
├── civitai_client.py           # Civitai API client
├── state_store.py              # per-user seen_ids / pushed_ids state
├── telegram_media.py           # Telegram upload / compression pipeline
├── media_compress.py           # lossless-first compression ladder
├── config_io.py                # config.yaml read/write
├── load_env.py                 # .env loader
├── backfill-memory-wrapper.py  # backfill launch wrapper (os.execv passthrough to monitor.py)
├── config.yaml.example         # annotated config template
├── requirements.txt / requirements.lock
├── assets/                     # logo (dark & light)
├── tests/                      # pytest suite
└── .github/workflows/          # CI (lint + tests)

# Runtime files (git-ignored):
#   config.yaml · interval.json · active_backfills.json
#   civitai_cookies.txt · seen_ids/ · downloads/ · monitor.log*
```

Local testing:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest tests/
```

</details>

## Disclaimer

> For **educational and personal use only**. Downloaded media belongs to its
> creators; comply with Civitai's
> [Terms of Service](https://civitai.com/terms-of-service) and each work's
> license. Do not redistribute downloaded content without the creator's
> explicit permission.

## License

[MIT](LICENSE) © dorokuma
